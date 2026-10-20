"""Unweighted geometric-mean curves of ORIGINAL observed per-window H/V on the physical Hz grid."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .spectra import SpectralData


@dataclass
class MeanCurve:
    hv: np.ndarray  # NaN where invalid
    ln_mean: np.ndarray
    log_std: np.ndarray  # sample std (ddof=1) of ln H/V; NaN with < 2 windows
    count: np.ndarray
    valid: np.ndarray
    n_windows: int


def geometric_mean_curve(sd: SpectralData, rows: np.ndarray) -> MeanCurve:
    """exp(mean ln H/V) over the given windows, bin by bin, using only windows valid at that bin.

    A bin of the mean is valid when the grid point is valid and at least half of the windows (and at
    least one) are valid there. No weights, no alignment, no normalization.
    """
    rows = np.asarray(rows, dtype=np.int64)
    n = rows.size
    ng = sd.grid.freq.size
    if n == 0:
        nan = np.full(ng, np.nan)
        return MeanCurve(nan, nan.copy(), nan.copy(), np.zeros(ng, dtype=np.int64), np.zeros(ng, dtype=bool), 0)
    L = sd.ln_hv[rows]
    V = sd.valid[rows]
    count = V.sum(axis=0)
    Lz = np.where(V, L, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ln_mean = Lz.sum(axis=0) / count
        dev = np.where(V, L - ln_mean, 0.0)
        log_std = np.sqrt((dev * dev).sum(axis=0) / (count - 1))
    need = max(1, math.ceil(0.5 * n))
    valid = sd.grid.valid & (count >= need)
    ln_mean = np.where(valid, ln_mean, np.nan)
    log_std = np.where(valid & (count >= 2), log_std, np.nan)
    with np.errstate(invalid="ignore"):
        hv = np.exp(ln_mean)
    return MeanCurve(hv, ln_mean, log_std, count.astype(np.int64), valid, n)
