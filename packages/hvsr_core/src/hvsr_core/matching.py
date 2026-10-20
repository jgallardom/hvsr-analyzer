"""Peak-aligned template matching by residual R-squared in log amplitude, and greedy selection.

Algorithms: sdof-r2log-q129-v1 (matching), sdof-greedy-nonoverlap-v1 (selection).

For each candidate window and peak family:
  1. fp, Ap from the fixed peak estimator (frozen; independent of any bank).
  2. Fit band [fp/2, 2 fp] must be valid for that window and for the family's common band.
  3. Enough original FFT bins inside the fit band (resolution check).
  4. y_j = ln(HV(fp q_j) / Ap) by linear interpolation of ln HV in ln f (no overshoot).
  5. R2_log(k) = 1 - sum w (y - t_k)^2 / sum w (y - ybar)^2 for every template k (batched).
Final curves are ALWAYS computed from the original observed spectra (curves.geometric_mean_curve);
nothing in this module produces averaged q-domain or aligned spectra.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .config import AnalysisConfig
from .curves import MeanCurve, geometric_mean_curve
from .peaks import Family, parabolic_peak, strict_max_in_band
from .spectra import SpectralData
from .templates import X_GRID, TemplateBank
from .windows import WindowTable

STATUSES = (
    "eligible",
    "no_meaningful_match",
    "no_peak_in_family_band",
    "incomplete_fit_band",
    "outside_common_band",
    "insufficient_resolution",
    "undefined_r2",
)


@dataclass
class PeakEstimates:
    """Bank-independent per-window peak estimates for one family (frozen before matching)."""

    rows: np.ndarray
    fp: np.ndarray  # NaN when no peak
    ln_ap: np.ndarray
    grid_index: np.ndarray  # -1 when no peak


def estimate_peaks(
    ln_hv: np.ndarray, valid: np.ndarray, ln_f: np.ndarray, rows: np.ndarray, lo: int, hi: int, method: str
) -> PeakEstimates:
    fp = np.full(rows.size, np.nan)
    la = np.full(rows.size, np.nan)
    gi = np.full(rows.size, -1, dtype=np.int64)
    for k, r in enumerate(rows):
        i = strict_max_in_band(ln_hv[r], valid[r], lo, hi)
        if i is None:
            continue
        gi[k] = i
        if method == "parabolic_log":
            f, a = parabolic_peak(ln_f, ln_hv[r], i)
        else:
            f, a = float(math.exp(ln_f[i])), float(ln_hv[r, i])
        fp[k], la[k] = f, a
    return PeakEstimates(rows, fp, la, gi)


def _bracket(ln_f: np.ndarray, lo_val: float, hi_val: float) -> tuple[int, int] | None:
    """Grid indices [a, b] whose span covers [lo_val, hi_val] in ln f (a <= lo, b >= hi)."""
    if lo_val < ln_f[0] - 1e-12 or hi_val > ln_f[-1] + 1e-12:
        return None
    a = int(np.searchsorted(ln_f, lo_val + 1e-12, side="right") - 1)
    b = int(np.searchsorted(ln_f, hi_val - 1e-12, side="left"))
    return max(a, 0), min(b, ln_f.size - 1)


def fft_bins_between(
    f_lo: float, f_hi: float, df: float, lower_closed: bool = True, upper_closed: bool = True
) -> int:
    k_lo = math.ceil(f_lo / df - 1e-9) if lower_closed else math.floor(f_lo / df + 1e-9) + 1
    k_hi = math.floor(f_hi / df + 1e-9) if upper_closed else math.ceil(f_hi / df - 1e-9) - 1
    return max(0, k_hi - max(k_lo, 1) + 1)


@dataclass
class MatchTable:
    family_id: str
    bank: dict
    rows: np.ndarray
    peaks: PeakEstimates
    status: list[str]
    y: np.ndarray  # (n, 129) NaN for unrankable
    r2: np.ndarray  # (n, K) NaN where undefined
    best_k: np.ndarray  # -1 when undefined
    best_r2: np.ndarray
    flags: list[list[str]]
    fit_band_hz: np.ndarray  # (n, 2)

    def eligible_mask(self) -> np.ndarray:
        return np.array([s == "eligible" for s in self.status])

    def status_counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for s in self.status:
            out[s] += 1
        return out


def batched_sse(Y: np.ndarray, T: np.ndarray, w: np.ndarray) -> np.ndarray:
    """sum_j w_j (y_ij - t_kj)^2 for all i, k via squared norms and dot products."""
    yy = (Y * Y) @ w
    tt = (T * T) @ w
    yt = (Y * w) @ T.T
    return np.maximum(yy[:, None] - 2.0 * yt + tt[None, :], 0.0)


def match_family(
    ln_hv: np.ndarray,
    valid: np.ndarray,
    ln_f: np.ndarray,
    df_hz: float,
    rows: np.ndarray,
    family_id: str,
    lo: int,
    hi: int,
    bank: TemplateBank,
    config: AnalysisConfig,
    peaks: PeakEstimates | None = None,
) -> MatchTable:
    cfg = config.sdof
    if peaks is None:
        peaks = estimate_peaks(ln_hv, valid, ln_f, rows, lo, hi, cfg.peak_interpolation)
    n = rows.size
    status = ["eligible"] * n
    flags: list[list[str]] = [[] for _ in range(n)]
    Y = np.full((n, X_GRID.size), np.nan)
    fit = np.full((n, 2), np.nan)
    ln2 = math.log(2.0)
    h = ln_f[1] - ln_f[0]
    common = _bracket(ln_f, ln_f[lo] - 0.5 * h - ln2, ln_f[hi] + 0.5 * h + ln2)
    for k, r in enumerate(rows):
        if peaks.grid_index[k] < 0:
            status[k] = "no_peak_in_family_band"
            continue
        lfp = math.log(peaks.fp[k])
        fit[k] = (peaks.fp[k] / 2.0, 2.0 * peaks.fp[k])
        br = _bracket(ln_f, lfp - ln2, lfp + ln2)
        if br is None or not valid[r, br[0] : br[1] + 1].all():
            status[k] = "incomplete_fit_band"
            continue
        if common is None or not valid[r, common[0] : common[1] + 1].all():
            status[k] = "outside_common_band"
            continue
        if (
            fft_bins_between(fit[k, 0], fit[k, 1], df_hz) < cfg.min_fft_bins_in_fit_band
            or fft_bins_between(fit[k, 0], peaks.fp[k], df_hz, upper_closed=False)
            < cfg.min_fft_bins_below_peak
        ):
            status[k] = "insufficient_resolution"
            continue
        a, b = br
        Y[k] = np.interp(lfp + X_GRID, ln_f[a : b + 1], ln_hv[r, a : b + 1]) - peaks.ln_ap[k]
    w = bank.weights
    T = bank.log_t
    K = T.shape[0]
    r2 = np.full((n, K), np.nan)
    best_k = np.full(n, -1, dtype=np.int64)
    best = np.full(n, np.nan)
    ok = np.array([s == "eligible" for s in status])
    if ok.any():
        Yo = Y[ok]
        ybar = Yo @ w
        sst = ((Yo - ybar[:, None]) ** 2) @ w
        sse = batched_sse(Yo, T, w)
        with np.errstate(divide="ignore", invalid="ignore"):
            r2o = 1.0 - sse / sst[:, None]
        idx = np.flatnonzero(ok)
        for j, k in enumerate(idx):
            if not sst[j] >= cfg.min_log_variance:
                status[k] = "undefined_r2"
                continue
            row = r2o[j]
            kb = int(np.argmax(row))
            # exact residual for the reported best template (avoids cancellation in the batched form)
            direct = float(((Yo[j] - T[kb]) ** 2) @ w)
            r2[k] = row
            r2[k, kb] = 1.0 - direct / sst[j]
            best_k[k] = kb
            best[k] = r2[k, kb]
            if np.sum(row == row[kb]) > 1:
                flags[k].append("tie")
            if bank.boundary_mask(kb):
                flags[k].append("grid_boundary_best")
            if not best[k] > cfg.r2_min:
                status[k] = "no_meaningful_match"
    return MatchTable(family_id, bank.provenance(), rows, peaks, status, Y, r2, best_k, best, flags, fit)


def greedy_select(
    global_start: np.ndarray, ids: list[str], n_samples: int, score: np.ndarray, eligible: np.ndarray
) -> list[int]:
    """Indices (into the arrays) chosen best-first; every candidate sharing a sample is removed.

    Order: higher score first, then lower global start index, then stable window ID.
    """
    cand = [i for i in range(score.size) if eligible[i]]
    cand.sort(key=lambda i: (-score[i], int(global_start[i]), ids[i]))
    chosen: list[int] = []
    taken: list[int] = []
    for i in cand:
        s = int(global_start[i])
        if any(s < t + n_samples and t < s + n_samples for t in taken):
            continue
        chosen.append(i)
        taken.append(s)
    return chosen


@dataclass
class FamilySelection:
    family_id: str
    bank: dict
    table: MatchTable
    selected: np.ndarray  # window rows in greedy order
    status: str
    requested_n: int | None
    mean: MeanCurve
    notes: list[str] = field(default_factory=list)

    def selected_rows_sorted(self) -> np.ndarray:
        return np.sort(self.selected)


def select_family(
    table: MatchTable, win: WindowTable, sd: SpectralData, config: AnalysisConfig
) -> FamilySelection:
    cfg = config.sdof
    ids = win.ids()
    gs = win.global_start[table.rows]
    elig = table.eligible_mask()
    order = greedy_select(
        gs, [ids[r] for r in table.rows], win.n_samples, np.nan_to_num(table.best_r2, nan=-np.inf), elig
    )
    notes = []
    requested = None
    if cfg.selection_mode == "manual_top_n":
        requested = cfg.manual_n
        order = order[:requested]
        notes.append("manual top-n selection: results are conditional on this selection rule")
    sel = table.rows[np.asarray(order, dtype=np.int64)] if order else np.zeros(0, dtype=np.int64)
    if sel.size == 0:
        status = "no_eligible_windows"
    elif requested is not None and sel.size < requested:
        status = "insufficient_matches"
        notes.append(f"requested {requested} windows, only {sel.size} eligible non-overlapping matches exist")
    else:
        status = "ok"
    mean = geometric_mean_curve(sd, sel)  # ORIGINAL observed spectra, physical Hz grid, unweighted
    return FamilySelection(table.family_id, table.bank, table, sel, status, requested, mean, notes)


def sdof_for_families(
    sd: SpectralData,
    qc_passed: np.ndarray,
    families: list[Family],
    bank: TemplateBank,
    config: AnalysisConfig,
) -> list[FamilySelection]:
    """Rank and select separately inside each plausible peak family."""
    rows = np.flatnonzero(qc_passed)
    ln_hv = sd.ln_hv
    ln_f = np.log(sd.grid.freq)
    out = []
    for fam in families:
        if not fam.plausible:
            continue
        table = match_family(
            ln_hv, sd.valid, ln_f, sd.df_hz, rows, fam.family_id, fam.lo_index, fam.hi_index, bank, config
        )
        out.append(select_family(table, sd.window, sd, config))
    return out
