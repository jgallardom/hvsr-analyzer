"""Offline Monte Carlo validation harness (mc-harness-v1).

Estimands, scenarios, seeds, metrics and acceptance targets are frozen in a protocol document that is
hash-locked before the locked test set runs (docs/validation/). Truth curves come from separate truth
seeds. Development, test and truth seed sets are disjoint integer ranges.

Two estimands are kept apart:
- the selection-policy mean curve (what the CI targets): the expected ln H/V of windows retained by
  the method's policy, estimated by pooling retained windows over many independent truth records;
- the known physical resonance (transfer-function peak frequency and ln|T| amplitude), which no
  selection-conditional CI promises to cover.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..config import AnalysisConfig
from ..core import run_core
from ..matching import sdof_for_families
from ..selection import search_band_mask
from ..templates import resolve_sdof_bank
from ..uncertainty import selection_uncertainty
from .synthetic import TFSpec, generate_3c, transfer_amplitude

HARNESS_VERSION = "mc-harness-v1"
DURATION_S = 1800.0
FS_HZ = 50.0
SET_BASE = {"dev": 1_000_000, "test": 2_000_000, "truth": 3_000_000}
MATCH_DECADES = 0.1


@dataclass(frozen=True)
class Scenario:
    name: str
    categories: tuple[str, ...]
    tf: TFSpec
    gen: dict = field(default_factory=dict)
    eval_hz: float | None = None  # predeclared pointwise-coverage frequency for null scenarios
    regime: dict | None = None  # {"span_s", "alt_tf": TFSpec, "p"}
    random_transients: dict | None = None  # {"count", "amplitude_rel", "duration_s"}
    source_hz: tuple[float, ...] = ()  # known non-site lines (machine tones)

    @property
    def null(self) -> bool:
        return self.tf.kind == "flat"

    def true_peaks_hz(self, fmin: float = 0.2, fmax: float = 20.0) -> list[float]:
        if self.null:
            return []
        f = np.logspace(math.log10(fmin), math.log10(fmax), 20001)
        a = transfer_amplitude(self.tf, f)
        i = np.flatnonzero((a[1:-1] > a[:-2]) & (a[1:-1] >= a[2:])) + 1
        return [float(f[k]) for k in i if a[k] >= 1.5]

    @property
    def fundamental_hz(self) -> float | None:
        p = self.true_peaks_hz()
        return p[0] if p else None

    def coverage_hz(self) -> float:
        return self.fundamental_hz if self.fundamental_hz is not None else float(self.eval_hz)

    def describe(self) -> dict:
        d = {
            "name": self.name,
            "categories": list(self.categories),
            "transfer": self.tf.as_dict(),
            "generator": self.gen,
            "true_peaks_hz": [round(p, 6) for p in self.true_peaks_hz()],
            "fundamental_hz": None if self.fundamental_hz is None else round(self.fundamental_hz, 6),
            "coverage_frequency_hz": round(self.coverage_hz(), 6),
            "known_source_lines_hz": list(self.source_hz),
        }
        if self.regime:
            d["regime_switching"] = {**self.regime, "alt_tf": self.regime["alt_tf"].as_dict()}
        if self.random_transients:
            d["random_transients"] = self.random_transients
        return d


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("flat_white", ("flat", "null"), TFSpec("flat"), eval_hz=2.0),
    Scenario(
        "flat_colored_episodes",
        ("flat", "null", "colored_noise", "temporal_correlation"),
        TFSpec("flat"),
        {"noise_alpha": 1.5, "episodes": {"tau_s": 120.0, "sigma_ln": 0.7}},
        eval_hz=2.0,
    ),
    Scenario("sdof_2hz", ("sdof_in_model",), TFSpec("sdof", {"fn": 2.0, "zeta": 0.15})),
    Scenario(
        "broad_asymmetric_3hz",
        ("broad", "asymmetric", "non_sdof"),
        TFSpec(
            "asymmetric_peak",
            {"fc": 3.0, "amplitude": 3.5, "width_left_decades": 0.25, "width_right_decades": 0.08},
        ),
    ),
    Scenario(
        "two_peaks",
        ("multiple_peaks", "non_sdof"),
        TFSpec(
            "multi_peak",
            {
                "peaks": [
                    {"fc": 1.5, "amplitude": 4.0, "width_decades": 0.06},
                    {"fc": 6.0, "amplitude": 4.0, "width_decades": 0.06},
                ]
            },
        ),
    ),
    Scenario(
        "weak_fundamental",
        ("weak_fundamental", "multiple_peaks", "non_sdof"),
        TFSpec(
            "multi_peak",
            {
                "peaks": [
                    {"fc": 1.0, "amplitude": 2.2, "width_decades": 0.06},
                    {"fc": 6.0, "amplitude": 6.0, "width_decades": 0.06},
                ]
            },
        ),
    ),
    Scenario(
        "regime_switching",
        ("temporal_correlation", "non_sdof"),
        TFSpec("lognormal_peak", {"fc": 2.0, "amplitude": 4.0, "width_decades": 0.1}),
        regime={
            "span_s": 300.0,
            "p": 0.5,
            "alt_tf": TFSpec("lognormal_peak", {"fc": 2.0, "amplitude": 2.5, "width_decades": 0.1}),
        },
    ),
    Scenario(
        "colored_sdof",
        ("colored_noise", "temporal_correlation", "sdof_in_model"),
        TFSpec("sdof", {"fn": 1.2, "zeta": 0.2}),
        {"noise_alpha": 1.5, "episodes": {"tau_s": 120.0, "sigma_ln": 0.7}},
    ),
    Scenario(
        "transients",
        ("transients", "non_sdof"),
        TFSpec("lognormal_peak", {"fc": 2.5, "amplitude": 4.0, "width_decades": 0.1}),
        random_transients={"count": 12, "amplitude_rel": 50.0, "duration_s": 2.0},
    ),
    Scenario(
        "colored_mild_sdof",
        ("colored_noise", "sdof_in_model"),
        TFSpec("sdof", {"fn": 1.2, "zeta": 0.2}),
        {"noise_alpha": 0.75},
    ),
    Scenario(
        "episodes_lognormal",
        ("temporal_correlation", "non_sdof"),
        TFSpec("lognormal_peak", {"fc": 2.0, "amplitude": 4.0, "width_decades": 0.1}),
        {"episodes": {"tau_s": 120.0, "sigma_ln": 0.7}},
    ),
    Scenario(
        "machine_tone",
        ("machine_tone", "non_sdof"),
        TFSpec("lognormal_peak", {"fc": 2.0, "amplitude": 3.5, "width_decades": 0.1}),
        {"tones": [{"freq_hz": 7.5, "amplitude_rel": 0.5, "components": "NE"}]},
        source_hz=(7.5,),
    ),
    Scenario(
        "component_mismatch",
        ("component_mismatch", "sdof_in_model"),
        TFSpec("sdof", {"fn": 3.0, "zeta": 0.15}),
        {"horizontal_gain_mismatch": 1.3},
    ),
    Scenario(
        "layered_sh",
        ("layered_site_response", "non_sdof"),
        TFSpec(
            "layered_sh",
            {
                "layers": [{"vs": 200.0, "h": 25.0, "rho": 1800.0, "damping": 0.02}],
                "halfspace": {"vs": 800.0, "rho": 2200.0, "damping": 0.005},
            },
        ),
    ),
)
SCENARIO_BY_NAME = {s.name: s for s in SCENARIOS}


def seeds(set_name: str, scenario_index: int, n: int) -> list[int]:
    base = SET_BASE[set_name] + scenario_index * 10_000
    if n > 10_000:
        raise ValueError("at most 10000 replicates per scenario and set")
    return [base + i for i in range(n)]


def make_record(sc: Scenario, seed: int):
    rng = np.random.default_rng([seed, 99])
    kw = dict(sc.gen)
    if sc.regime:
        spans = []
        n_spans = int(math.ceil(DURATION_S / sc.regime["span_s"]))
        for k in range(n_spans):
            if rng.random() < sc.regime["p"]:
                spans.append(
                    {
                        "start_s": k * sc.regime["span_s"],
                        "end_s": (k + 1) * sc.regime["span_s"],
                        "tf": sc.regime["alt_tf"],
                    }
                )
        kw["regimes"] = spans
    if sc.random_transients:
        rt = sc.random_transients
        times = np.sort(rng.uniform(5.0, DURATION_S - 5.0, size=rt["count"]))
        kw["transients"] = [
            {"time_s": float(t), "amplitude_rel": rt["amplitude_rel"], "duration_s": rt["duration_s"]}
            for t in times
        ]
    return generate_3c(duration_s=DURATION_S, fs=FS_HZ, tf=sc.tf, seed=seed, **kw)


def _matches(fam, peak_hz: float) -> bool:
    ref = fam.mean_peak_hz if fam.mean_peak_hz is not None else fam.center_hz
    return (fam.lo_hz <= peak_hz <= fam.hi_hz) or abs(math.log10(ref / peak_hz)) <= MATCH_DECADES


def _matched_family(fams, peak_hz: float | None):
    if peak_hz is None:
        return None
    cands = [
        f for f in fams if f.plausible and "possible_source_peak" not in f.flags and _matches(f, peak_hz)
    ]
    if not cands:
        return None
    return min(cands, key=lambda f: abs(math.log10((f.mean_peak_hz or f.center_hz) / peak_hz)))


@dataclass
class _Run:
    core: object
    families: list
    sdof: list
    bank: object
    times: dict


def _analyze(sc: Scenario, seed: int, config: AnalysisConfig) -> _Run:
    rec, _ = make_record(sc, seed)
    t0 = time.perf_counter()
    core = run_core(rec, config)
    t1 = time.perf_counter()
    bank, _ = resolve_sdof_bank(config.sdof.template_bank, config.spectral.ko_bandwidth)
    sel = sdof_for_families(core.spectra, core.qc.passed, core.discovery.families, bank, config)
    t2 = time.perf_counter()
    return _Run(core, core.discovery.families, sel, bank, {"core_s": t1 - t0, "sdof_match_s": t2 - t1})


def truth_contribution(sc: Scenario, seed: int, config: AnalysisConfig) -> dict:
    """Pooled sums of ln H/V over policy-retained windows of one truth record."""
    r = _analyze(sc, seed, config)
    sd = r.core.spectra
    out = {}

    def acc(rows):
        L = sd.ln_hv[rows]
        V = sd.valid[rows] & sd.grid.valid
        return np.where(V, L, 0.0).sum(axis=0), V.sum(axis=0)

    s, c = acc(np.sort(r.core.standard.rows))
    out["standard"] = (s, c)
    fam = _matched_family(r.families, sc.fundamental_hz)
    sel = next((x for x in r.sdof if fam is not None and x.family_id == fam.family_id), None)
    if sel is not None and sel.selected.size:
        out["sdof"] = acc(np.sort(sel.selected))
    out["freq"] = sd.grid.freq
    return out


def pool_truth(contribs: list[dict]) -> dict:
    freq = contribs[0]["freq"]
    res = {"freq_hz": freq}
    for key in ("standard", "sdof"):
        parts = [c[key] for c in contribs if key in c]
        if not parts:
            res[key] = None
            continue
        s = np.sum([p[0] for p in parts], axis=0)
        n = np.sum([p[1] for p in parts], axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            res[key] = {
                "ln_mean": np.where(n > 0, s / np.maximum(n, 1), np.nan),
                "windows_at_bin": n,
                "records": len(parts),
            }
    return res


def _cov(lo, hi, idx, truth_ln):
    if truth_ln is None or not np.isfinite(truth_ln) or not np.isfinite(lo[idx]) or not np.isfinite(hi[idx]):
        return None
    return bool(lo[idx] <= truth_ln <= hi[idx])


def replicate(sc: Scenario, seed: int, config: AnalysisConfig, truth: dict) -> dict:
    r = _analyze(sc, seed, config)
    core = r.core
    sd = core.spectra
    disc = core.discovery
    f = sd.grid.freq
    ln_f = np.log(f)
    band = search_band_mask(sd, config)
    idx = int(np.argmin(np.abs(ln_f - math.log(sc.coverage_hz()))))
    true_peaks = sc.true_peaks_hz()
    f0 = sc.fundamental_hz
    phys_ln = float(np.log(transfer_amplitude(sc.tf, np.array([f[idx]]))[0]))
    if "horizontal_gain_mismatch" in sc.gen:
        g = sc.gen["horizontal_gain_mismatch"]
        phys_ln += 0.5 * math.log((1 + g * g) / 2)  # RMS-combined horizontal of mismatched components

    plaus = [fm for fm in disc.families if fm.plausible]
    non_source = [fm for fm in plaus if "possible_source_peak" not in fm.flags]
    false_fams = [fm for fm in non_source if not any(_matches(fm, p) for p in true_peaks)]
    source_hits = [fm for fm in plaus if "possible_source_peak" in fm.flags]
    out: dict = {
        "scenario": sc.name,
        "seed": seed,
        "times": dict(r.times),
        "discovery": {
            "status": disc.status,
            "n_plausible": len(plaus),
            "plausible_peaks_hz": [fm.mean_peak_hz for fm in plaus],
            "source_flagged_hz": [fm.mean_peak_hz for fm in source_hits],
            "false_peak": bool(false_fams),
            "n_false_peaks": len(false_fams),
            "fundamental_hz": disc.fundamental_hz,
            "fundamental_correct": None
            if f0 is None
            else (
                disc.fundamental_hz is not None and abs(math.log10(disc.fundamental_hz / f0)) <= MATCH_DECADES
            ),
        },
        "coverage_frequency_hz": float(f[idx]),
    }
    fund_fam = next((fm for fm in disc.families if fm.family_id == disc.fundamental_family_id), None)
    matched = _matched_family(disc.families, f0)

    def method_block(rows, mean, fam, truth_key, sel=None):
        t = time.perf_counter()
        su = selection_uncertainty(sd, core.prep, rows, mean, config, band, fam, disc.families)
        dt = time.perf_counter() - t
        ci = su.ci
        tr = truth.get(truth_key)
        truth_ln = None if tr is None else float(tr["ln_mean"][idx])
        blk = {
            "n_windows": int(np.asarray(rows).size),
            "ci_status": ci.status,
            "informative_blocks": ci.info.get("informative_blocks"),
            "block_length_s": ci.info.get("block_length_s"),
            "ci_width_ln_at_eval": None
            if not np.isfinite(ci.upper_ln[idx] - ci.lower_ln[idx])
            else float(ci.upper_ln[idx] - ci.lower_ln[idx]),
            "covers_policy_truth": _cov(ci.lower_ln, ci.upper_ln, idx, truth_ln),
            "covers_physical_amplitude": _cov(ci.lower_ln, ci.upper_ln, idx, phys_ln),
            "uncertainty_s": dt,
        }
        pk = su.peak
        fp = pk.get("observed_peak_frequency_hz")
        blk["fp_hz"] = fp
        blk["fp_ln_error"] = None if (fp is None or f0 is None) else math.log(fp / f0)
        b = pk.get("bootstrap") or {}
        ivl = b.get("conditional_interval_hz")
        blk["peak_interval_covers_f0"] = None if (ivl is None or f0 is None) else bool(ivl[0] <= f0 <= ivl[1])
        blk["peak_fraction_no_peak"] = b.get("fraction_no_peak_in_family")
        blk["peak_multimodal"] = None if not b else b.get("status") == "ambiguous_multimodal"
        if sel is not None:
            tbl = sel.table
            in_std = np.isin(tbl.rows, core.standard.rows)
            sel_mask = np.isin(tbl.rows, sel.selected)
            blk["selection_status"] = sel.status
            blk["mean_r2_selected"] = float(np.nanmean(tbl.best_r2[sel_mask])) if sel_mask.any() else None
            blk["mean_r2_standard_windows"] = float(np.nanmean(tbl.best_r2[in_std])) if in_std.any() else None
            blk["median_r2_selected"] = float(np.nanmedian(tbl.best_r2[sel_mask])) if sel_mask.any() else None
        return blk

    # Standard: inferred-fundamental family (what a user gets); null scenarios: no family
    std_fam = fund_fam if fund_fam is not None else matched
    out["standard"] = method_block(np.sort(core.standard.rows), disc.mean, std_fam, "standard")
    out["standard"]["family"] = None if std_fam is None else std_fam.family_id
    # SDOF: selection inside the family matching the true fundamental (selected-regime estimand)
    sel = next((x for x in r.sdof if matched is not None and x.family_id == matched.family_id), None)
    if sel is not None and sel.selected.size:
        out["sdof"] = method_block(np.sort(sel.selected), sel.mean, matched, "sdof", sel)
        out["sdof"]["family"] = matched.family_id
    else:
        out["sdof"] = {
            "n_windows": 0,
            "ci_status": "no_selection",
            "selection_status": None if sel is None else sel.status,
            "family": None if matched is None else matched.family_id,
        }
    # R-squared vs fundamental (AC-S5-05)
    r2_by_family = {
        x.family_id: float(np.nanmedian(x.table.best_r2[np.isin(x.table.rows, x.selected)]))
        for x in r.sdof
        if x.selected.size
    }
    best_r2_fam = max(r2_by_family, key=r2_by_family.get) if r2_by_family else None
    out["r2_vs_fundamental"] = {
        "median_r2_by_family": r2_by_family,
        "r2_preferred_family": best_r2_fam,
        "r2_preferred_family_wrong": None
        if (best_r2_fam is None or matched is None or len(r2_by_family) < 2)
        else best_r2_fam != matched.family_id,
        "inferred_fundamental_wrong": None if f0 is None else not out["discovery"]["fundamental_correct"],
    }
    s, d = out["standard"], out["sdof"]
    if (
        d.get("fp_ln_error") is not None
        and s.get("fp_ln_error") is not None
        and d.get("mean_r2_selected") is not None
    ):
        out["r2_vs_fundamental"]["sdof_r2_up_fp_worse"] = bool(
            d["mean_r2_selected"] > (d["mean_r2_standard_windows"] or -np.inf)
            and abs(d["fp_ln_error"]) > abs(s["fp_ln_error"])
        )
    else:
        out["r2_vs_fundamental"]["sdof_r2_up_fp_worse"] = None
    return out
