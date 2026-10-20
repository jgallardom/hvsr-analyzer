"""Basic invalid-data checks (basic-qc-v1): clipping and STA/LTA anti-trigger screening.

These checks are shared by every selection method (spec 8.1/8.2). They never alter samples.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import AnalysisConfig
from .model import Recording
from .preprocess import Prepared
from .windows import WindowTable

REASONS = ("clipped", "dead_channel", "sta_lta_out_of_range")


@dataclass
class WindowQC:
    passed: np.ndarray
    reasons: list[tuple[str, ...]]
    notes: list[tuple[str, ...]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {r: 0 for r in REASONS}
        for rs in self.reasons:
            for r in rs:
                out[r] = out.get(r, 0) + 1
        return out


def _prefix(mask: np.ndarray) -> np.ndarray:
    return np.concatenate(([0], np.cumsum(mask, dtype=np.int64)))


def screen_windows(rec: Recording, prep: Prepared, win: WindowTable, config: AnalysisConfig) -> WindowQC:
    """Apply basic-qc-v1 to every candidate window. Never alters samples."""
    q = config.basic_qc
    n = win.n_samples
    per_seg: dict[int, dict[str, np.ndarray]] = {}
    for pos, pseg in enumerate(prep.segments):
        raw_seg = rec.segments[pseg.segment_index]
        off = pseg.global_start - raw_seg.start_index
        raw = raw_seg.data[:, off : off + pseg.data.shape[1]]
        clip_p, dead_p, sl_bad_p, sl_def_p = [], [], [], []
        for c in range(3):
            fsc = rec.channels[c].full_scale
            if q.clip_detection:
                cm = clipping_mask(raw[c], fsc, q.clip_fraction_of_full_scale, q.flat_top_run_samples)
            else:
                cm = np.zeros(raw.shape[1], dtype=bool)
            clip_p.append(_prefix(cm))
            dead_p.append(_prefix(np.diff(raw[c], prepend=raw[c][:1]) != 0))
            if q.sta_lta_enabled:
                ratio = sta_lta_ratio(pseg.data[c], prep.fs, q.sta_s, q.lta_s)
                defined = np.isfinite(ratio)
                with np.errstate(invalid="ignore"):
                    bad = defined & ((ratio < q.sta_lta_min) | (ratio > q.sta_lta_max))
            else:
                defined = np.ones(raw.shape[1], dtype=bool)
                bad = np.zeros(raw.shape[1], dtype=bool)
            sl_bad_p.append(_prefix(bad))
            sl_def_p.append(_prefix(defined))
        per_seg[pos] = {
            "clip": np.vstack(clip_p),
            "change": np.vstack(dead_p),
            "slbad": np.vstack(sl_bad_p),
            "sldef": np.vstack(sl_def_p),
        }
    passed = np.ones(win.count, dtype=bool)
    reasons: list[tuple[str, ...]] = []
    notes: list[tuple[str, ...]] = []
    for k in range(win.count):
        p = per_seg[int(win.segment_pos[k])]
        s = int(win.local_start[k])
        e = s + n
        rs = []
        if np.any(p["clip"][:, e] - p["clip"][:, s] > 0):
            rs.append("clipped")
        # samples that differ from their predecessor inside (s, e): zero -> constant channel
        if np.any(p["change"][:, e] - p["change"][:, s + 1] == 0):
            rs.append("dead_channel")
        if np.any(p["slbad"][:, e] - p["slbad"][:, s] > 0):
            rs.append("sta_lta_out_of_range")
        nt = []
        if q.sta_lta_enabled and np.any(p["sldef"][:, e] - p["sldef"][:, s] < n):
            nt.append("sta_lta_partially_evaluated")
        reasons.append(tuple(rs))
        notes.append(tuple(nt))
        passed[k] = not rs
    return WindowQC(passed, reasons, notes)


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
