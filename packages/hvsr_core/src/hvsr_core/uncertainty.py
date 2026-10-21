"""Dependence-aware temporal block bootstrap of the selected-window mean (temporal-block-bootstrap-v2)
and peak-replicate outcomes (peak-replicate-v1).

Estimand: the frequency-by-frequency geometric mean of the windows retained by the declared selection
policy (selection-conditional). Replicates resample WHOLE window spectra (all frequencies together),
average in ln amplitude, and back-transform. Only accepted windows enter; rejected periods contribute
nothing and no rejection-rate factor exists. The 95 % level is nominal and uncalibrated.

Decisions D-050 (revised) and D-051 in docs/scientific-decisions.md.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np

from .config import AnalysisConfig, Uncertainty
from .curves import MeanCurve
from .peaks import Family, parabolic_peak, strict_max_in_band
from .preprocess import Prepared
from .spectra import SpectralData

LABEL = "Estimated 95% pointwise CI for the selected-window mean"
CALIBRATION_NOTE = (
    "Nominal 95 %; coverage not calibrated for this configuration (see Stage 5 validation report)."
)


@dataclass
class BlockPlan:
    method: str  # circular_moving_block | original_time_cluster
    block_length_s: float
    block_length_windows: int | None
    n_blocks: int
    clusters: np.ndarray | None  # cluster id per (time-sorted) window for the cluster bootstrap
    diagnostics: dict = field(default_factory=dict)


def is_regular(times_s: np.ndarray, segment_ids: np.ndarray, max_ratio: float) -> bool:
    if times_s.size < 3:
        return False
    if np.unique(segment_ids).size > 1:
        return False
    d = np.diff(times_s)
    return bool(d.min() > 0 and d.max() / d.min() <= max_ratio)


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def dependence_test(means: np.ndarray, pairs: np.ndarray, cfg: Uncertainty) -> dict:
    """Lag-1 correlation of time-adjacent block means, per feature.

    Dependent when max r exceeds BOTH the materiality threshold and the one-sided Bonferroni bound
    z(1 - alpha/p)/sqrt(pairs). Fewer than 4 adjacent pairs: not assessable.
    """
    from scipy.stats import norm

    p = means.shape[1]
    out: dict = {
        "adjacent_pairs": int(pairs.size),
        "max_lag1_corr": None,
        "critical_r": None,
        "dependent": None,
    }
    if pairs.size < 4 or p == 0:
        return out
    cs = [c for c in (_corr(means[pairs, j], means[pairs + 1, j]) for j in range(p)) if c is not None]
    if not cs:
        return out
    crit = max(
        cfg.adjacent_block_corr_max,
        float(norm.ppf(1 - cfg.dependence_test_alpha / p)) / math.sqrt(pairs.size),
    )
    out.update(max_lag1_corr=max(cs), critical_r=crit, dependent=bool(max(cs) > crit))
    return out


def _pw_block(feats: np.ndarray) -> tuple[int, dict]:
    from arch.bootstrap import optimal_block_length

    est = {}
    best = 1.0
    for j in range(feats.shape[1]):
        x = feats[:, j]
        if not np.all(np.isfinite(x)) or np.std(x) == 0:
            est[f"feature_{j}"] = None
            continue
        v = float(optimal_block_length(x)["circular"].iloc[0])
        est[f"feature_{j}"] = v
        if np.isfinite(v):
            best = max(best, v)
    return max(1, math.ceil(best)), est


def _finish_diag(diag: dict, hist: list[dict], doublings: int) -> dict:
    last = hist[-1]
    diag["adjacent_block_correlation"] = hist
    diag["doublings"] = doublings
    diag["dependence_assessable"] = last["dependent"] is not None
    diag["dependence_resolved"] = last["dependent"] is False
    if last["critical_r"] is not None and last["critical_r"] > 0.5:
        diag["low_power_note"] = (
            f"with {last['adjacent_pairs']} adjacent block pairs only lag-1 correlations above {last['critical_r']:.2f} are detectable"
        )
    return diag


def plan_regular(
    times_s: np.ndarray, feats: np.ndarray, cfg: Uncertainty, factor: float = 1.0, base: int | None = None
) -> BlockPlan:
    n = times_s.size
    spacing = float(np.median(np.diff(times_s)))
    if base is None:
        b, est = _pw_block(feats)
        diag: dict = {
            "estimator": "Politis-White (2004) with Patton-Politis-White (2009) correction, circular, arch.bootstrap.optimal_block_length; max over features",
            "per_feature_block_windows": est,
        }
        doublings = 0
        hist = []
        while True:
            nb = n // b
            means = (
                np.vstack([feats[k * b : (k + 1) * b].mean(axis=0) for k in range(nb)])
                if nb
                else np.zeros((0, feats.shape[1]))
            )
            t = dependence_test(means, np.arange(max(nb - 1, 0)), cfg)
            hist.append({"block_windows": b, "n_blocks": nb, **t})
            if t["dependent"] and doublings < cfg.max_block_doublings:
                b *= 2
                doublings += 1
                continue
            break
        diag = _finish_diag(diag, hist, doublings)
    else:
        b = base
        diag = {}
    b = min(max(1, int(round(b * factor))), n)
    return BlockPlan("circular_moving_block", b * spacing, b, n // b, None, diag)


def plan_original_time(
    times_s: np.ndarray,
    origin_s: float,
    lw_s: float,
    feats: np.ndarray,
    cfg: Uncertainty,
    factor: float = 1.0,
    base_s: float | None = None,
) -> BlockPlan:
    def clusters_for(length: float) -> np.ndarray:
        return np.floor((times_s - origin_s) / length + 1e-9).astype(np.int64)

    if base_s is None:
        lb = cfg.irregular_block_lw_factor * lw_s
        doublings = 0
        hist = []
        while True:
            cl = clusters_for(lb)
            ids = np.unique(cl)
            means = np.vstack([feats[cl == c].mean(axis=0) for c in ids])
            t = dependence_test(
                means, np.flatnonzero(np.diff(ids) == 1), cfg
            )  # truly adjacent non-empty blocks
            hist.append({"block_length_s": lb, "n_blocks": int(ids.size), **t})
            if t["dependent"] and doublings < cfg.max_block_doublings:
                lb *= 2
                doublings += 1
                continue
            break
        diag = _finish_diag(
            {
                "estimator": f"original-time blocks of {cfg.irregular_block_lw_factor:g} x Lw anchored at the analyzed start, doubled while adjacent blocks are dependent"
            },
            hist,
            doublings,
        )
    else:
        lb = base_s
        diag = {}
    lb = lb * factor
    cl = clusters_for(lb)
    _, inv = np.unique(cl, return_inverse=True)
    return BlockPlan("original_time_cluster", lb, None, int(inv.max() + 1), inv, diag)


def replicate_counts(plan: BlockPlan, n: int, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    if plan.method == "circular_moving_block":
        b = plan.block_length_windows
        m = math.ceil(n / b)
        starts = rng.integers(0, n, size=(n_boot, m))
        idx = ((starts[:, :, None] + np.arange(b)[None, None, :]) % n).reshape(n_boot, m * b)[:, :n]
    else:
        k = plan.n_blocks
        draws = rng.integers(0, k, size=(n_boot, k))
        cc = np.zeros((n_boot, k), dtype=np.int64)
        np.add.at(cc, (np.repeat(np.arange(n_boot), k), draws.ravel()), 1)
        return cc[:, plan.clusters]
    flat = idx + (np.arange(n_boot) * n)[:, None]
    return np.bincount(flat.ravel(), minlength=n_boot * n).reshape(n_boot, n)


def replicate_means(L: np.ndarray, V: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """ln geometric means per replicate: sum_w c_w L_w / sum_w c_w over windows valid at each bin."""
    Lz = np.where(V, L, 0.0)
    num = counts @ Lz
    den = counts @ V.astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def expanded_levels(k: int) -> tuple[float, float]:
    """Expanded percentile levels (Hesterberg 2015) for k resampling units: Phi^-1(q) = -sqrt(k/(k-1)) t_{0.975,k-1}."""
    from scipy.stats import norm, t

    if k < 2:
        return 0.025, 0.975
    q = float(norm.cdf(-math.sqrt(k / (k - 1)) * t.ppf(0.975, k - 1)))
    return q, 1.0 - q


def band_from_replicates(
    reps: np.ndarray, mean_valid: np.ndarray, k: int
) -> tuple[np.ndarray, np.ndarray, int]:
    if reps.size == 0:
        nan = np.full(reps.shape[1], np.nan)
        return nan, nan.copy(), 0
    masked = np.where(mean_valid, reps, np.nan)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns at invalid bins
        lo, hi = np.nanquantile(masked, list(expanded_levels(k)), axis=0)
    zero = mean_valid & np.isfinite(lo) & np.isfinite(hi) & (hi - lo <= 1e-12)
    lo = np.where(mean_valid & ~zero, lo, np.nan)
    hi = np.where(mean_valid & ~zero, hi, np.nan)
    return lo, hi, int(zero.sum())


def dependence_features(
    L: np.ndarray, V: np.ndarray, ln_f: np.ndarray, peak_index: int, window_ln_fp: np.ndarray | None
) -> np.ndarray:
    """D-051 predeclared features per window (rows sorted by time)."""
    f_p = ln_f[peak_index]
    ln2, s2 = math.log(2.0), 0.5 * math.log(2.0)
    lo_band = (ln_f >= f_p - ln2) & (ln_f <= f_p - s2)
    hi_band = (ln_f >= f_p + s2) & (ln_f <= f_p + ln2)
    cols = [np.where(V[:, peak_index], L[:, peak_index], np.nan)]
    for m in (lo_band, hi_band):
        if m.any():
            with np.errstate(invalid="ignore"):
                cols.append(np.nanmean(np.where(V[:, m], L[:, m], np.nan), axis=1))
    if window_ln_fp is not None and np.all(np.isfinite(window_ln_fp)):
        cols.append(window_ln_fp)
    F = np.column_stack(cols)
    # features with any missing value are not usable for dependence assessment
    keep = np.all(np.isfinite(F), axis=0)
    return F[:, keep]


def _width(lo: np.ndarray, hi: np.ndarray, mask: np.ndarray) -> float | None:
    w = (hi - lo)[mask & np.isfinite(lo) & np.isfinite(hi)]
    return float(np.median(w)) if w.size else None


@dataclass
class UncertaintyResult:
    status: str
    status_reason: str | None
    lower_ln: np.ndarray
    upper_ln: np.ndarray
    replicates: np.ndarray | None
    info: dict

    def as_dict(self) -> dict:
        return self.info


def selected_mean_ci(
    L: np.ndarray,
    V: np.ndarray,
    mean_valid: np.ndarray,
    times_s: np.ndarray,
    segment_ids: np.ndarray,
    origin_s: float,
    lw_s: float,
    feats: np.ndarray,
    width_mask: np.ndarray,
    cfg: Uncertainty,
    n_boot: int | None = None,
) -> UncertaintyResult:
    """Block bootstrap of the selected-window mean. Rows of L/V/feats must be sorted by time."""
    n_boot = n_boot or cfg.n_bootstrap
    ng = L.shape[1] if L.ndim == 2 else 0
    nan = np.full(ng, np.nan)
    base_info = {
        "label": LABEL,
        "selection_conditional": True,
        "coverage_calibrated": False,
        "calibration_note": CALIBRATION_NOTE,
        "seed": cfg.seed,
        "n_bootstrap": n_boot,
        "resampling_unit": "whole window spectra (all frequencies jointly), ln-amplitude averaging",
        "interval": "expanded percentile (Hesterberg 2015) with k = informative blocks",
        "excludes": [
            "sensor/response uncertainty",
            "template/model mismatch",
            "peak-family ambiguity",
            "parameter/selection sensitivity",
        ],
    }
    n = times_s.size
    if n < 2 or feats.shape[1] == 0:
        info = dict(
            base_info,
            status="ci_not_estimable",
            status_reason=f"{n} windows / no usable dependence features",
            method="none",
            block_length_s=None,
            informative_blocks=None,
            dependence=None,
            sensitivity=None,
        )
        return UncertaintyResult("ci_not_estimable", info["status_reason"], nan, nan.copy(), None, info)
    regular = is_regular(times_s, segment_ids, cfg.regular_spacing_max_ratio)
    use_pw = regular and n >= cfg.pw_min_windows
    plan = (
        plan_regular(times_s, feats, cfg)
        if use_pw
        else plan_original_time(times_s, origin_s, lw_s, feats, cfg)
    )
    reasons: list[str] = []
    notes: list[str] = []
    if regular and not use_pw:
        notes.append(
            f"regular sequence with {n} < {cfg.pw_min_windows} windows: original-time blocks used (Politis-White is unstable at this size)"
        )
    if plan.diagnostics.get("low_power_note"):
        notes.append(plan.diagnostics["low_power_note"])
    status = "ci_estimated"
    if plan.n_blocks < cfg.min_informative_blocks:
        reason = f"{plan.n_blocks} informative blocks (< {cfg.min_informative_blocks}, experimental guard)"
        info = dict(
            base_info,
            status="ci_not_estimable",
            status_reason=reason,
            method=plan.method,
            block_length_s=plan.block_length_s,
            informative_blocks=plan.n_blocks,
            dependence=plan.diagnostics,
            sensitivity=None,
        )
        return UncertaintyResult("ci_not_estimable", reason, nan, nan.copy(), None, info)
    rng = np.random.default_rng(cfg.seed)
    reps = replicate_means(L, V, replicate_counts(plan, n, n_boot, rng))
    lo, hi, zero_bins = band_from_replicates(reps, mean_valid, plan.n_blocks)
    if zero_bins:
        reasons.append(f"{zero_bins} bins had zero bootstrap width and are reported as null")
        status = "ci_limited"
    if not plan.diagnostics["dependence_assessable"]:
        reasons.append("too few adjacent block pairs to assess dependence")
        status = "ci_limited"
    elif not plan.diagnostics["dependence_resolved"]:
        reasons.append("adjacent blocks remain dependent after the maximum number of doublings")
        status = "ci_limited"
    base_w = _width(lo, hi, width_mask)
    sens = [
        {
            "factor": 1.0,
            "block_length_s": plan.block_length_s,
            "informative_blocks": plan.n_blocks,
            "median_width_ln": base_w,
            "status": "estimated",
        }
    ]
    widths = [base_w] if base_w is not None else []
    for fct in cfg.sensitivity_factors:
        if use_pw:
            p2 = plan_regular(times_s, feats, cfg, factor=fct, base=plan.block_length_windows)
        else:
            p2 = plan_original_time(
                times_s, origin_s, lw_s, feats, cfg, factor=fct, base_s=plan.block_length_s
            )
        if p2.n_blocks < cfg.min_informative_blocks:
            sens.append(
                {
                    "factor": fct,
                    "block_length_s": p2.block_length_s,
                    "informative_blocks": p2.n_blocks,
                    "median_width_ln": None,
                    "status": "not_estimable",
                }
            )
            continue
        r2 = replicate_means(L, V, replicate_counts(p2, n, n_boot, np.random.default_rng(cfg.seed)))
        l2, h2, _ = band_from_replicates(r2, mean_valid, p2.n_blocks)
        w2 = _width(l2, h2, width_mask)
        sens.append(
            {
                "factor": fct,
                "block_length_s": p2.block_length_s,
                "informative_blocks": p2.n_blocks,
                "median_width_ln": w2,
                "status": "estimated",
            }
        )
        if w2 is not None:
            widths.append(w2)
    ratio = (max(widths) / min(widths)) if len(widths) >= 2 and min(widths) > 0 else None
    if ratio is not None and ratio > cfg.sensitivity_width_ratio_limit:
        reasons.append(
            f"band width changes by a factor {ratio:.2f} across predeclared block lengths (> {cfg.sensitivity_width_ratio_limit})"
        )
        status = "ci_limited"
    skipped = [s["factor"] for s in sens if s["status"] == "not_estimable"]
    if skipped:
        notes.append(
            f"block-length sensitivity not estimable at factor(s) {skipped} (< {cfg.min_informative_blocks} blocks)"
        )
    if len(widths) < 2:
        reasons.append("block-length sensitivity could not be assessed at any alternative predeclared length")
        status = "ci_limited"
    info = dict(
        base_info,
        status=status,
        status_reason="; ".join(reasons) or None,
        method=plan.method,
        block_length_s=plan.block_length_s,
        block_length_windows=plan.block_length_windows,
        informative_blocks=plan.n_blocks,
        regular_sequence=regular,
        notes=notes,
        dependence=plan.diagnostics,
        sensitivity=sens,
        sensitivity_width_ratio=ratio,
        n_windows=n,
        effective_sample_size_diagnostic=plan.n_blocks,
    )
    return UncertaintyResult(status, info["status_reason"], lo, hi, reps, info)


def peak_replicates(
    reps: np.ndarray,
    mean_valid: np.ndarray,
    ln_f: np.ndarray,
    family: tuple[str, int, int],
    other_families: list[tuple[str, int, int]],
    search_band: np.ndarray,
    min_mode_mass: float = 0.1,
) -> dict:
    """Peak of each replicate mean inside the family; no realignment. Tracks every outcome."""
    fid, lo, hi = family
    B = reps.shape[0]
    fps = np.full(B, np.nan)
    no_peak = 0
    alternate = 0
    for k in range(B):
        y = reps[k]
        v = mean_valid & np.isfinite(y)
        i = strict_max_in_band(y, v, lo, hi)
        if i is None:
            no_peak += 1
        else:
            fps[k] = parabolic_peak(ln_f, y, i)[0]
        m = search_band & v
        if m.any():
            g = int(np.nanargmax(np.where(m, y, -np.inf)))
            if not (lo <= g <= hi) and any(a <= g <= b for _, a, b in other_families):
                alternate += 1
    found = fps[np.isfinite(fps)]
    out = {
        "method": "peak-replicate-v1: strict local maximum of each replicate mean inside the family band; no realignment",
        "n_replicates": int(B),
        "fraction_peak_in_family": float(found.size / B) if B else 0.0,
        "fraction_no_peak_in_family": float(no_peak / B) if B else 0.0,
        "fraction_global_max_in_other_family": float(alternate / B) if B else 0.0,
        "conditioning": f"replicates whose mean curve has a strict local maximum inside family {fid}",
    }
    if found.size < 20:
        out.update(status="not_estimable", conditional_interval_hz=None, modes_hz=[])
        return out
    lf = np.log(found)
    sd = float(np.std(lf))
    h = max(1.06 * sd * found.size ** (-0.2), (ln_f[1] - ln_f[0]))
    grid = np.linspace(lf.min() - 3 * h, lf.max() + 3 * h, 512)
    dens = np.exp(-0.5 * ((grid[:, None] - lf[None, :]) / h) ** 2).sum(axis=1)
    from scipy.signal import find_peaks

    pk = find_peaks(np.concatenate(([0.0], dens, [0.0])))[0] - 1
    masses = []
    if pk.size > 1:
        cuts = [grid[pk[j] + int(np.argmin(dens[pk[j] : pk[j + 1] + 1]))] for j in range(pk.size - 1)]
        edges = np.concatenate(([-np.inf], cuts, [np.inf]))
        masses = [float(np.mean((lf > edges[j]) & (lf <= edges[j + 1]))) for j in range(pk.size)]
        major = [j for j, m in enumerate(masses) if m >= min_mode_mass]
    else:
        major = [0]
        masses = [1.0]
    q = np.quantile(found, [0.025, 0.975])
    out.update(
        status="ambiguous_multimodal" if len(major) > 1 else "unimodal",
        conditional_interval_hz=[float(q[0]), float(q[1])],
        modes_hz=[float(math.exp(grid[pk[j]])) for j in major],
        mode_masses=[masses[j] for j in major],
        median_hz=float(np.median(found)),
    )
    return out


def window_ln_peak(sd: SpectralData, rows: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """ln of each window's strict in-family peak frequency (parabolic refinement); NaN when absent."""
    ln_f = np.log(sd.grid.freq)
    L = sd.ln_hv
    out = np.full(rows.size, np.nan)
    for k, r in enumerate(rows):
        v = sd.valid[r] & sd.grid.valid
        i = strict_max_in_band(L[r], v, lo, hi)
        if i is not None:
            out[k] = math.log(parabolic_peak(ln_f, np.where(v, L[r], np.nan), i)[0])
    return out


@dataclass
class SelectionUncertainty:
    ci: UncertaintyResult
    peak: dict
    dispersion: dict


def selection_uncertainty(
    sd: SpectralData,
    prep: Prepared,
    rows: np.ndarray,
    mean: MeanCurve,
    config: AnalysisConfig,
    search_band: np.ndarray,
    family: Family | None = None,
    families: list[Family] | None = None,
    n_boot: int | None = None,
) -> SelectionUncertainty:
    """CI of a selection's geometric mean + observed peak + peak-replicate outcomes + window dispersion."""
    cfg = config.uncertainty
    rows = np.sort(np.asarray(rows, dtype=np.int64))
    ln_f = np.log(sd.grid.freq)
    win = sd.window
    L = sd.ln_hv[rows]
    V = sd.valid[rows] & sd.grid.valid
    times = win.global_start[rows] / prep.fs
    segs = win.segment_pos[rows]
    origin = prep.analyzed_start_index / prep.fs
    lw_s = win.n_samples / prep.fs
    m = search_band & mean.valid
    peak_idx = None
    obs = None
    if family is not None:
        i = strict_max_in_band(mean.ln_mean, mean.valid, family.lo_index, family.hi_index)
        if i is not None:
            peak_idx = i
            obs = parabolic_peak(ln_f, mean.ln_mean, i)
    if peak_idx is None and m.any():
        peak_idx = int(np.nanargmax(np.where(m, mean.ln_mean, -np.inf)))
    wfp = (
        window_ln_peak(sd, rows, family.lo_index, family.hi_index)
        if family is not None and rows.size
        else None
    )
    if peak_idx is None or rows.size == 0:
        feats = np.zeros((rows.size, 0))
    else:
        feats = dependence_features(L, V, ln_f, peak_idx, wfp)
    ci = selected_mean_ci(L, V, mean.valid, times, segs, origin, lw_s, feats, m, cfg, n_boot=n_boot)
    ci.info["dependence_features"] = int(feats.shape[1])

    peak: dict = {"peak_family_id": family.family_id if family is not None else None}
    if family is None:
        peak.update(
            status="not_evaluable",
            observed_peak_frequency_hz=None,
            observed_peak_amplitude=None,
            window_peak_frequency_hz=None,
            bootstrap=None,
        )
    else:
        if obs is None:
            peak.update(
                status="no_peak_in_family_band", observed_peak_frequency_hz=None, observed_peak_amplitude=None
            )
        else:
            peak.update(
                status="peak_found",
                observed_peak_frequency_hz=float(obs[0]),
                observed_peak_amplitude=float(math.exp(obs[1])),
            )
        finite = wfp[np.isfinite(wfp)] if wfp is not None else np.zeros(0)
        peak["window_peak_frequency_hz"] = {
            "geometric_mean": float(math.exp(finite.mean())) if finite.size else None,
            "log_std": float(np.std(finite, ddof=1)) if finite.size >= 2 else None,
            "n_windows_with_peak": int(finite.size),
            "n_windows": int(rows.size),
            "note": "dispersion of per-window peaks; not a confidence interval",
        }
        if ci.replicates is not None:
            others = [
                (f.family_id, f.lo_index, f.hi_index)
                for f in (families or [])
                if f.family_id != family.family_id
            ]
            peak["bootstrap"] = peak_replicates(
                ci.replicates,
                mean.valid,
                ln_f,
                (family.family_id, family.lo_index, family.hi_index),
                others,
                search_band,
            )
        else:
            peak["bootstrap"] = None
    disp = {
        "label": "Window-to-window dispersion (sample std of ln H/V); describes variability, not the uncertainty of the mean",
        "median_log_std_in_search_band": float(np.nanmedian(mean.log_std[m]))
        if np.any(m & np.isfinite(mean.log_std))
        else None,
    }
    return SelectionUncertainty(ci, peak, disp)


def export_curve(mean: MeanCurve, ci: UncertaintyResult) -> dict:
    """One-band export contract: the mean, ONE CI band, counts, and dispersion labelled separately."""

    def arr(x):
        return [None if not np.isfinite(v) else float(v) for v in x]

    with np.errstate(invalid="ignore"):
        lo = np.exp(ci.lower_ln)
        hi = np.exp(ci.upper_ln)
    return {
        "hv_geometric_mean": arr(mean.hv),
        "ci95_lower": arr(lo),
        "ci95_upper": arr(hi),
        "count": [int(c) for c in mean.count],
        "log_std": arr(mean.log_std),
    }
