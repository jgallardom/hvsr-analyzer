"""Fourier amplitude spectra, Konno-Ohmachi smoothing operator, analysis grid (fas-ko-sparse-v1)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy import signal, sparse

from .config import AnalysisConfig
from .preprocess import Prepared
from .windows import WindowTable

INVALID_REASONS = (
    "above_anti_alias_limit",
    "insufficient_smoothing_bins",
    "outside_declared_usable_band",
)


@dataclass
class Grid:
    points_per_decade: int
    index: np.ndarray  # integer exponents i, f = 10**(i/P)
    freq: np.ndarray
    valid: np.ndarray
    invalid_reason: list[str | None]
    smoothing_bins: np.ndarray
    effective_bins: np.ndarray

    def reason_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.invalid_reason:
            if r:
                out[r] = out.get(r, 0) + 1
        return out


def grid_indices(fmin: float, fmax: float, ppd: int) -> np.ndarray:
    lo = math.ceil(ppd * math.log10(fmin) - 1e-9)
    hi = math.floor(ppd * math.log10(fmax) + 1e-9)
    return np.arange(lo, hi + 1, dtype=np.int64)


def fft_frequencies(n: int, fs: float) -> np.ndarray:
    return np.fft.rfftfreq(n, 1.0 / fs)


@lru_cache(maxsize=32)
def _ko_operator_cached(n: int, fs: float, grid_key: tuple, b: float, support: float):
    ppd, i0, i1 = grid_key
    fc = 10.0 ** (np.arange(i0, i1 + 1) / ppd)
    f = fft_frequencies(n, fs)
    upper = 10.0 ** (support / b)
    lower = 10.0 ** (-support / b)
    data, indices, indptr = [], [], [0]
    counts = np.zeros(fc.size, dtype=np.int64)
    eff = np.zeros(fc.size)
    for j, c in enumerate(fc):
        lo = np.searchsorted(f, c * lower, side="left")
        hi = np.searchsorted(f, c * upper, side="right")
        lo = max(lo, 1)  # exclude DC
        ff = f[lo:hi]
        if ff.size:
            ratio = ff / c
            keep = (ratio >= lower) & (ratio <= upper)
            ff = ff[keep]
            idx = np.arange(lo, hi)[keep]
        else:
            idx = np.arange(0)
        if ff.size:
            x = b * np.log10(ff / c)
            with np.errstate(divide="ignore", invalid="ignore"):
                w = np.where(np.abs(ff - c) < 1e-6, 1.0, (np.sin(x) / x) ** 4)
            s = w.sum()
            counts[j] = ff.size
            eff[j] = s * s / np.sum(w * w) if s > 0 else 0.0
            data.append(w / s)
            indices.append(idx)
        indptr.append(indptr[-1] + ff.size)
    mat = sparse.csr_matrix(
        (np.concatenate(data) if data else np.zeros(0), np.concatenate(indices) if indices else np.zeros(0, dtype=np.int64), np.asarray(indptr)),
        shape=(fc.size, f.size),
    )
    return mat, counts, eff


def ko_operator(n: int, fs: float, grid: np.ndarray, ppd: int, b: float, support: float):
    """Row-normalized sparse Konno-Ohmachi operator mapping rFFT bins -> grid frequencies."""
    key = (ppd, int(grid[0]), int(grid[-1]))
    return _ko_operator_cached(n, float(fs), key, float(b), float(support))


def ko_dense_reference(f: np.ndarray, amp: np.ndarray, fc: float, b: float, support: float) -> float:
    """Direct (loop) evaluation used only by tests to verify the sparse operator."""
    num = den = 0.0
    for fi, ai in zip(f, amp, strict=True):
        if fi < 1e-6:
            continue
        r = fi / fc
        if r > 10 ** (support / b) or r < 10 ** (-support / b):
            continue
        if abs(fi - fc) < 1e-6:
            w = 1.0
        else:
            x = b * math.log10(r)
            w = (math.sin(x) / x) ** 4
        num += w * ai
        den += w
    return num / den if den > 0 else float("nan")


def build_grid(config: AnalysisConfig, fs: float, n: int, usable_band: list[float | None]) -> Grid:
    sp = config.spectral
    ps = config.peak_search
    fmin = sp.grid_fmin_hz if sp.grid_fmin_hz is not None else ps.fmin_hz / 4.0
    fmax = sp.grid_fmax_hz if sp.grid_fmax_hz is not None else ps.fmax_hz * 4.0
    fmax = min(fmax, fs / 2.0)
    idx = grid_indices(fmin, fmax, sp.grid_points_per_decade)
    freq = 10.0 ** (idx / sp.grid_points_per_decade)
    _, counts, eff = ko_operator(n, fs, idx, sp.grid_points_per_decade, sp.ko_bandwidth, sp.ko_support_rad)
    reasons: list[str | None] = [None] * freq.size
    limit = sp.anti_alias_fraction * fs / 2.0
    lo_b, hi_b = usable_band
    for j, f in enumerate(freq):
        if f * 10 ** (sp.ko_support_rad / sp.ko_bandwidth) > limit:
            reasons[j] = "above_anti_alias_limit"
        elif counts[j] < sp.min_smoothing_bins:
            reasons[j] = "insufficient_smoothing_bins"
        elif (lo_b is not None and f < lo_b) or (hi_b is not None and f > hi_b):
            reasons[j] = "outside_declared_usable_band"
    valid = np.array([r is None for r in reasons])
    return Grid(sp.grid_points_per_decade, idx, freq, valid, reasons, counts, eff)


def window_amplitudes(prep: Prepared, win: WindowTable, rows: np.ndarray, config: AnalysisConfig) -> np.ndarray:
    """|rFFT| / fs of demeaned, detrended, tapered windows. Returns (3, len(rows), n_fft)."""
    n = win.n_samples
    pp = config.preprocessing
    taper = signal.windows.tukey(n, alpha=pp.taper_alpha) if pp.taper_alpha > 0 else np.ones(n)
    out = np.empty((3, rows.size, n // 2 + 1))
    for k, r in enumerate(rows):
        seg = prep.segments[int(win.segment_pos[r])].data
        s = int(win.local_start[r])
        x = seg[:, s : s + n]
        x = x - x.mean(axis=1, keepdims=True)
        if pp.detrend == "linear":
            x = signal.detrend(x, axis=1, type="linear")
        elif pp.detrend == "constant":
            x = signal.detrend(x, axis=1, type="constant")
        out[:, k, :] = np.abs(np.fft.rfft(x * taper, axis=1)) / prep.fs
    return out


@dataclass
class SpectralData:
    fs: float
    window: WindowTable
    grid: Grid
    n_fft: int
    df_hz: float
    H: np.ndarray
    V: np.ndarray
    N: np.ndarray
    E: np.ndarray
    hv: np.ndarray  # NaN where invalid
    valid: np.ndarray
    invalid_counts: dict[str, int]

    @property
    def ln_hv(self) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.log(self.hv)


def combine_horizontal(n_amp: np.ndarray, e_amp: np.ndarray, how: str) -> np.ndarray:
    if how == "rms":
        return np.sqrt((n_amp * n_amp + e_amp * e_amp) / 2.0)
    if how == "geometric":
        return np.sqrt(n_amp * e_amp)
    raise ValueError(how)


def compute_spectra(prep: Prepared, win: WindowTable, grid: Grid, config: AnalysisConfig, batch: int = 128) -> SpectralData:
    sp = config.spectral
    op, _, _ = ko_operator(win.n_samples, prep.fs, grid.index, sp.grid_points_per_decade, sp.ko_bandwidth, sp.ko_support_rad)
    opT = op.T.tocsr()
    nw, ng = win.count, grid.freq.size
    H = np.full((nw, ng), np.nan)
    V = np.full((nw, ng), np.nan)
    N = np.full((nw, ng), np.nan)
    E = np.full((nw, ng), np.nan)
    for b0 in range(0, nw, batch):
        rows = np.arange(b0, min(nw, b0 + batch))
        amp = window_amplitudes(prep, win, rows, config)
        h_raw = combine_horizontal(amp[0], amp[1], sp.horizontal_combination)
        H[rows] = h_raw @ opT
        V[rows] = amp[2] @ opT
        N[rows] = amp[0] @ opT
        E[rows] = amp[1] @ opT
    hv, valid, invalid_counts = guarded_ratio(H, V, grid.valid, sp.vertical_floor_rel)
    return SpectralData(prep.fs, win, grid, win.n_samples, prep.fs / win.n_samples, H, V, N, E, hv, valid, invalid_counts)


def guarded_ratio(H: np.ndarray, V: np.ndarray, grid_valid: np.ndarray, floor_rel: float):
    """H/V with invalid bins as NaN (D-014). Never substitutes a floor value for V."""
    H = np.atleast_2d(H)
    V = np.atleast_2d(V)
    valid = np.broadcast_to(grid_valid, H.shape).copy()
    counts = {"vertical_below_floor": 0, "nonpositive_or_nonfinite": 0}
    with np.errstate(invalid="ignore", divide="ignore", all="ignore"):
        ref = np.where(grid_valid & np.isfinite(V) & (V > 0), V, np.nan)
        all_nan = np.all(np.isnan(ref), axis=1)
        vmed = np.where(all_nan, np.inf, np.nanmedian(np.where(all_nan[:, None], 1.0, ref), axis=1))
        floor = floor_rel * vmed[:, None]
        bad_v = ~(np.isfinite(V) & (V > floor)) & valid
        bad_h = ~(np.isfinite(H) & (H > 0)) & valid
        counts["vertical_below_floor"] = int(bad_v.sum())
        counts["nonpositive_or_nonfinite"] = int((bad_h & ~bad_v).sum())
        valid &= ~bad_v & ~bad_h
        hv = np.where(valid, H / V, np.nan)
    return hv, valid, counts
