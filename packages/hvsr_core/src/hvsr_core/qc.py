"""Basic invalid-data checks (basic-qc-v1): clipping and STA/LTA anti-trigger screening.

These checks are shared by every selection method (spec 8.1/8.2). They never alter samples.
"""

from __future__ import annotations

import numpy as np


def clipping_mask(x: np.ndarray, full_scale: float | None, fraction: float, run_samples: int) -> np.ndarray:
    """Boolean mask of samples considered clipped.

    - With a declared full scale: |x| >= fraction * full_scale.
    - Always: runs of >= run_samples identical consecutive samples equal to the record maximum or
      minimum (flat-topping), which catches clipping when full scale is unknown.
    """
    x = np.asarray(x)
    mask = np.zeros(x.shape, dtype=bool)
    if x.size == 0:
        return mask
    if full_scale is not None:
        mask |= np.abs(x) >= fraction * full_scale
    for extreme in (x.max(), x.min()):
        hit = x == extreme
        if hit.sum() < run_samples:
            continue
        # run-length of consecutive hits
        d = np.diff(np.concatenate(([0], hit.astype(np.int8), [0])))
        starts = np.flatnonzero(d == 1)
        stops = np.flatnonzero(d == -1)
        for s, e in zip(starts, stops, strict=True):
            if e - s >= run_samples:
                mask[s:e] = True
    return mask


def sta_lta_ratio(x: np.ndarray, fs: float, sta_s: float, lta_s: float) -> np.ndarray:
    """Classic trailing STA/LTA on |x - mean(x)|. Values before the LTA is filled are NaN."""
    a = np.abs(np.asarray(x, dtype=float) - np.mean(x))
    nsta = max(1, int(round(sta_s * fs)))
    nlta = max(nsta + 1, int(round(lta_s * fs)))
    c = np.concatenate(([0.0], np.cumsum(a)))
    n = a.size
    ratio = np.full(n, np.nan)
    if n < nlta:
        return ratio
    idx = np.arange(nlta - 1, n)
    sta = (c[idx + 1] - c[idx + 1 - nsta]) / nsta
    lta = (c[idx + 1] - c[idx + 1 - nlta]) / nlta
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(lta > 0, sta / lta, np.nan)
    ratio[idx] = r
    return ratio
