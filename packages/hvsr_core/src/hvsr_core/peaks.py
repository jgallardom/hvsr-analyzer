"""Shape-neutral peak discovery and interpretation (peak-families-kde-v1, narrow-line-v1).

See docs/scientific-decisions.md D-030 ... D-033. All thresholds are research parameters from
config.peak_discovery, not SESAME constants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy import signal

from .config import AnalysisConfig
from .curves import MeanCurve, geometric_mean_curve
from .preprocess import Prepared
from .spectra import SpectralData, combine_horizontal, ko_operator, window_amplitudes

NON_FREE_FIELD_CONTEXTS = ("building_upper_floor", "structure_other")


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Half-open [start, stop) index runs where mask is True."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist(), strict=True))


def local_peaks(ln_y: np.ndarray, mask: np.ndarray, min_prom_ln: float) -> list[tuple[int, float]]:
    """Interior local maxima of ln_y inside contiguous runs of mask, with topographic prominence."""
    out = []
    for a, b in runs(mask):
        if b - a < 3:
            continue
        pk, props = signal.find_peaks(ln_y[a:b], prominence=min_prom_ln if min_prom_ln > 0 else (None, None))
        proms = props["prominences"] if "prominences" in props else signal.peak_prominences(ln_y[a:b], pk)[0]
        out.extend((a + int(i), float(p)) for i, p in zip(pk, proms, strict=True))
    return out


def parabolic_peak(ln_f: np.ndarray, ln_y: np.ndarray, i: int) -> tuple[float, float]:
    """3-point parabola in (ln f, ln y) on a uniform ln-f grid; offset bounded to +/- half a step."""
    if i <= 0 or i >= ln_y.size - 1 or not np.all(np.isfinite(ln_y[i - 1 : i + 2])):
        return float(math.exp(ln_f[i])), float(ln_y[i])
    y0, y1, y2 = ln_y[i - 1], ln_y[i], ln_y[i + 1]
    den = y0 - 2.0 * y1 + y2
    if den >= 0:
        return float(math.exp(ln_f[i])), float(y1)
    d = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
    h = ln_f[i + 1] - ln_f[i]
    return float(math.exp(ln_f[i] + d * h)), float(y1 - 0.25 * (y0 - y2) * d)


def strict_max_in_band(ln_y: np.ndarray, valid: np.ndarray, lo: int, hi: int) -> int | None:
    """Index of the maximum inside [lo, hi] if it is a strict local maximum of the full curve."""
    seg = np.where(valid[lo : hi + 1], ln_y[lo : hi + 1], -np.inf)
    if not np.any(np.isfinite(seg)):
        return None
    i = lo + int(np.argmax(seg))
    if i - 1 < 0 or i + 1 >= ln_y.size or not (valid[i - 1] and valid[i + 1]):
        return None
    if ln_y[i] > ln_y[i - 1] and ln_y[i] > ln_y[i + 1]:
        return i
    return None


@dataclass
class Family:
    family_id: str
    mode_index: int
    lo_index: int
    hi_index: int
    center_hz: float
    lo_hz: float
    hi_hz: float
    recurrence: float
    windows_with_peak: int
    mean_peak_hz: float | None = None
    mean_peak_amplitude: float | None = None
    mean_peak_index: int | None = None
    mean_prominence_ratio: float | None = None
    plausible: bool = False
    flags: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "family_id": self.family_id,
            "center_frequency_hz": self.center_hz,
            "lower_bound_hz": self.lo_hz,
            "upper_bound_hz": self.hi_hz,
            "recurrence": self.recurrence,
            "discovery_windows_with_peak": self.windows_with_peak,
            "mean_curve_peak_frequency_hz": self.mean_peak_hz,
            "mean_curve_peak_amplitude": self.mean_peak_amplitude,
            "mean_curve_prominence_ratio": self.mean_prominence_ratio,
            "plausible": self.plausible,
            "flags": list(self.flags),
            "evidence": self.evidence,
        }


@dataclass
class Discovery:
    rows: np.ndarray
    mean: MeanCurve
    families: list[Family]
    status: str
    flags: list[str]
    evidence: list[str]
    fundamental_family_id: str | None
    fundamental_hz: float | None
    global_max_hz: float | None
    global_max_amplitude: float | None
    window_peaks: list[tuple[int, int, float]]
    lower_band: dict

    def plausible_families(self) -> list[Family]:
        return [f for f in self.families if f.plausible]

    def interpretation(self) -> dict:
        return {
            "status": self.status,
            "fundamental_frequency_candidate_hz": self.fundamental_hz,
            "fundamental_family_id": self.fundamental_family_id,
            "evidence": list(self.evidence),
            "flags": list(self.flags),
            "human_annotations_url": None,
        }


def global_maximum(mean: MeanCurve, freq: np.ndarray, band: np.ndarray) -> tuple[float | None, float | None]:
    m = band & mean.valid
    if not m.any():
        return None, None
    i = int(np.nanargmax(np.where(m, mean.ln_mean, -np.inf)))
    return float(freq[i]), float(mean.hv[i])


def _kde_families(x: np.ndarray, xs: np.ndarray, cfg) -> list[tuple[int, int, int]]:
    """(mode, lo, hi) indices into xs after valley merging and half-width capping."""
    if x.size == 0:
        return []
    h = cfg.kde_bandwidth_decades
    dens = np.exp(-0.5 * ((xs[:, None] - x[None, :]) / h) ** 2).sum(axis=1)
    padded = np.concatenate(([0.0], dens, [0.0]))
    modes = (signal.find_peaks(padded)[0] - 1).tolist()
    if not modes:
        return []
    # minima between consecutive modes
    groups = [[modes[0]]]
    for m in modes[1:]:
        prev = groups[-1]
        lo = prev[-1]
        valley = lo + int(np.argmin(dens[lo : m + 1]))
        top = max(prev, key=lambda k: dens[k])
        if dens[valley] >= cfg.kde_merge_valley_ratio * min(dens[top], dens[m]):
            prev.append(m)
        else:
            groups.append([m])
    out = []
    bounds = []
    for gi, g in enumerate(groups):
        mode = max(g, key=lambda k: dens[k])
        left = 0 if gi == 0 else bounds[-1] + 1
        if gi + 1 < len(groups):
            nxt = groups[gi + 1][0]
            right = g[-1] + int(np.argmin(dens[g[-1] : nxt + 1]))
        else:
            right = xs.size - 1
        bounds.append(right)
        step = xs[1] - xs[0] if xs.size > 1 else 1.0
        cap = int(math.floor(cfg.family_max_halfwidth_decades / step + 1e-9))
        out.append((mode, max(left, mode - cap), min(right, mode + cap)))
    return out


def _source_diagnostic(
    prep: Prepared, sd: SpectralData, rows: np.ndarray, fam: Family, amps: np.ndarray, config: AnalysisConfig
) -> dict:
    cfg = config.peak_discovery
    f = np.fft.rfftfreq(sd.n_fft, 1.0 / sd.fs)
    with np.errstate(divide="ignore"):
        ln_amp = np.log(amps)  # (3, nr, nfft)
        ln_h = np.log(combine_horizontal(amps[0], amps[1], config.spectral.horizontal_combination))
    gm = ln_amp.mean(axis=1)  # geometric mean per component
    gm_h = ln_h.mean(axis=0)
    inband = np.flatnonzero((f >= fam.lo_hz) & (f <= fam.hi_hz) & (f > 0))
    ev: dict = {"method": "narrow-line-v1"}
    if inband.size == 0:
        ev["status"] = "no_fft_bins_in_family"
        return ev
    k = int(inband[np.argmax(gm_h[inband])])
    fk = f[k]
    w = cfg.narrow_line_max_width_bins
    neigh = np.flatnonzero((f >= 0.8 * fk) & (f <= 1.2 * fk) & (np.abs(np.arange(f.size) - k) > w) & (f > 0))
    ev["line_frequency_hz"] = float(fk)
    if neigh.size < 5:
        ev["status"] = "insufficient_neighbourhood"
        return ev
    contrast = {c: float(np.exp(gm[i, k] - np.median(gm[i, neigh]))) for i, c in enumerate("NEZ")}
    thr = math.log(2.0)
    lo = k
    while lo - 1 > 0 and gm_h[lo - 1] >= gm_h[k] - thr:
        lo -= 1
    hi = k
    while hi + 1 < f.size and gm_h[hi + 1] >= gm_h[k] - thr:
        hi += 1
    width = hi - lo + 1
    per_window = np.exp(ln_h[:, k] - np.median(ln_h[:, neigh], axis=1))
    persistence = float(np.mean(per_window >= cfg.narrow_line_min_contrast_ratio))
    harmonics = {}
    for m in (2, 3):
        km = int(round(m * fk / (f[1] - f[0])))
        if km + 2 >= f.size:
            continue
        nb = np.flatnonzero((f >= 0.8 * m * fk) & (f <= 1.2 * m * fk) & (np.abs(np.arange(f.size) - km) > w))
        if nb.size < 5:
            continue
        peak = gm_h[km - 1 : km + 2].max()
        harmonics[str(m)] = float(np.exp(peak - np.median(gm_h[nb])))
    # smoothing sensitivity: family peak amplitude of the discovery mean with b_alt vs configured b
    sp = config.spectral
    op_alt, _, _ = ko_operator(
        sd.n_fft,
        sd.fs,
        sd.grid.index,
        sp.grid_points_per_decade,
        cfg.smoothing_sensitivity_bandwidth,
        sp.ko_support_rad,
    )
    h_raw = combine_horizontal(amps[0], amps[1], sp.horizontal_combination)
    with np.errstate(divide="ignore", invalid="ignore"):
        ln_alt = np.log((h_raw @ op_alt.T.tocsr()) / (amps[2] @ op_alt.T.tocsr())).mean(axis=0)
    band = slice(fam.lo_index, fam.hi_index + 1)
    alt_vals = ln_alt[band][np.isfinite(ln_alt[band]) & sd.grid.valid[band]]
    base_vals = fam.evidence.get("_mean_ln_band")
    sens = None
    if alt_vals.size and base_vals is not None and np.isfinite(base_vals):
        sens = float(np.exp(alt_vals.max() - base_vals))
    n_comp = sum(c >= cfg.narrow_line_min_contrast_ratio for c in contrast.values())
    flagged = n_comp >= 2 and width <= w and persistence >= cfg.narrow_line_min_persistence
    ev.update(
        {
            "status": "evaluated",
            "component_contrast": contrast,
            "components_above_contrast": int(n_comp),
            "line_width_bins": int(width),
            "persistence": persistence,
            "harmonic_contrast": harmonics,
            "smoothing_sensitivity_ratio": sens,
            "smoothing_sensitivity_bandwidth": cfg.smoothing_sensitivity_bandwidth,
            "possible_source_peak": bool(flagged),
        }
    )
    return ev


def discover(
    prep: Prepared, sd: SpectralData, rows: np.ndarray, config: AnalysisConfig, context: str = "unknown"
) -> Discovery:
    cfg = config.peak_discovery
    rows = np.asarray(rows, dtype=np.int64)
    f = sd.grid.freq
    ln_f = np.log(f)
    band = (f >= config.peak_search.fmin_hz) & (f <= config.peak_search.fmax_hz)
    mean = geometric_mean_curve(sd, rows)
    gmax_f, gmax_a = global_maximum(mean, f, band)
    lower = {"lowest_usable_frequency_hz": None, "unresolved": False}
    if rows.size < cfg.min_discovery_windows or not (band & sd.grid.valid).any():
        reason = (
            f"only {rows.size} quality-screened non-overlapping windows (< {cfg.min_discovery_windows})"
            if rows.size < cfg.min_discovery_windows
            else "no valid grid frequency inside the search band"
        )
        return Discovery(
            rows, mean, [], "not_computable", [], [reason], None, None, gmax_f, gmax_a, [], lower
        )

    ln_hv = sd.ln_hv
    min_prom = math.log(cfg.window_min_prominence_ratio)
    window_peaks: list[tuple[int, int, float]] = []
    for r in rows:
        for i, p in local_peaks(ln_hv[r], band & sd.valid[r], min_prom):
            window_peaks.append((int(r), i, p))

    band_idx = np.flatnonzero(band)
    xs = np.log10(f[band_idx])
    x = np.log10(f[[i for _, i, _ in window_peaks]]) if window_peaks else np.zeros(0)
    fams: list[Family] = []
    mean_peaks = local_peaks(mean.ln_mean, band & mean.valid, 0.0)
    for mode, lo, hi in _kde_families(x, xs, cfg):
        gl, gh, gm = int(band_idx[lo]), int(band_idx[hi]), int(band_idx[mode])
        hit_rows = {r for r, i, _ in window_peaks if gl <= i <= gh}
        rec = len(hit_rows) / rows.size
        if rec < cfg.family_listing_min_recurrence:
            continue
        fam = Family("", gm, gl, gh, float(f[gm]), float(f[gl]), float(f[gh]), rec, len(hit_rows))
        inside = [(i, p) for i, p in mean_peaks if gl <= i <= gh]
        if inside:
            i, p = max(inside, key=lambda t: (t[1], -t[0]))
            pf, pa = parabolic_peak(ln_f, mean.ln_mean, i)
            fam.mean_peak_index, fam.mean_peak_hz, fam.mean_peak_amplitude = i, pf, float(math.exp(pa))
            fam.mean_prominence_ratio = float(math.exp(p))
            fam.evidence["_mean_ln_band"] = float(mean.ln_mean[i])
        reasons = []
        if rec < cfg.min_recurrence:
            reasons.append(f"recurrence {rec:.2f} < {cfg.min_recurrence}")
        if fam.mean_prominence_ratio is None:
            reasons.append("no local maximum of the discovery mean curve inside the family")
        elif fam.mean_prominence_ratio < cfg.mean_min_prominence_ratio:
            reasons.append(
                f"mean-curve prominence ratio {fam.mean_prominence_ratio:.2f} < {cfg.mean_min_prominence_ratio}"
            )
        fam.plausible = not reasons
        fam.evidence["not_plausible_reasons"] = reasons
        fams.append(fam)
    fams.sort(key=lambda fm: fm.center_hz)
    for k, fam in enumerate(fams, start=1):
        fam.family_id = f"F{k}"

    need_amps = [fm for fm in fams if fm.plausible]
    if need_amps:
        amps = window_amplitudes(prep, sd.window, rows, config)
        for fam in need_amps:
            ev = _source_diagnostic(prep, sd, rows, fam, amps, config)
            fam.evidence["source_diagnostic"] = ev
            if ev.get("possible_source_peak"):
                fam.flags.append("possible_source_peak")
    for fam in fams:
        fam.evidence.pop("_mean_ln_band", None)

    # lower band (D-032)
    usable = np.flatnonzero(band & mean.valid)
    if usable.size:
        i0 = int(usable[0])
        f_low = float(f[i0])
        sub = usable[f[usable] <= 2.0 * f_low]
        arg = int(sub[np.argmax(mean.ln_mean[sub])])
        unresolved = arg == i0 and mean.hv[i0] >= cfg.lower_edge_min_hv
        lower = {
            "lowest_usable_frequency_hz": f_low,
            "hv_at_lowest": float(mean.hv[i0]),
            "unresolved": bool(unresolved),
        }

    flags: list[str] = []
    evidence: list[str] = []
    plaus = [fm for fm in fams if fm.plausible]
    non_source = [fm for fm in plaus if "possible_source_peak" not in fm.flags]
    if any("possible_source_peak" in fm.flags for fm in plaus):
        flags.append("possible_source_peak")
    if lower["unresolved"]:
        flags.append("lower_band_unresolved")
        evidence.append(
            f"discovery mean curve rises toward the lowest usable frequency {lower['lowest_usable_frequency_hz']:.4g} Hz "
            f"(H/V {lower['hv_at_lowest']:.2f}); a lower mode may lie outside coverage"
        )
    if lower["unresolved"]:
        status = "lower_band_unresolved"
    elif not non_source and plaus:
        status = "possible_source_peak"
    elif not non_source:
        status = "no_clear_peak"
    elif len(non_source) == 1:
        status = "single_plausible_peak"
    else:
        status = "multiple_plausible_peaks"
    for fm in plaus:
        evidence.append(
            f"{fm.family_id}: mean-curve peak {fm.mean_peak_hz:.4g} Hz, prominence ratio {fm.mean_prominence_ratio:.2f}, "
            f"recurrence {fm.recurrence:.2f}"
            + (" (possible source line)" if "possible_source_peak" in fm.flags else "")
        )
    fund_id = fund_hz = None
    if non_source and not lower["unresolved"]:
        if context in NON_FREE_FIELD_CONTEXTS:
            evidence.append(
                f"measurement context {context!r}: no free-field site-frequency candidate is reported"
            )
            flags.append("non_free_field_context")
        else:
            low = non_source[0]
            fund_id, fund_hz = low.family_id, low.mean_peak_hz
            evidence.append(f"fundamental candidate: lowest plausible non-source family {low.family_id}")
    if not plaus:
        evidence.append("no peak family meets the predeclared recurrence and mean-curve prominence rules")
    return Discovery(
        rows, mean, fams, status, flags, evidence, fund_id, fund_hz, gmax_f, gmax_a, window_peaks, lower
    )
