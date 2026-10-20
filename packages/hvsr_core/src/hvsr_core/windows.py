"""Candidate windows on an integer sample grid (win-halfopen-v1).

A window is the half-open interval ``[start, start + n)`` of global sample indices. Candidate starts
lie on the grid ``analyzed_start + k * step`` and every window lies entirely inside one segment, so
windows never span a gap. Window IDs are derived from the global start index and are stable for a
given recording, analyzed span, and step.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .preprocess import Prepared


@dataclass
class WindowTable:
    n_samples: int
    step_samples: int
    global_start: np.ndarray  # int64
    segment_pos: np.ndarray  # index into Prepared.segments
    local_start: np.ndarray  # int64, index into PreparedSegment.data

    @property
    def count(self) -> int:
        return int(self.global_start.size)

    @property
    def global_stop(self) -> np.ndarray:
        return self.global_start + self.n_samples

    def ids(self) -> list[str]:
        return [window_id(int(s)) for s in self.global_start]

    def start_time_s(self, fs: float) -> np.ndarray:
        return self.global_start / fs


def window_id(global_start: int) -> str:
    return f"w{global_start:010d}"


def sample_counts(fs: float, lw_s: float, step_s: float) -> tuple[int, int]:
    n = int(round(lw_s * fs))
    step = int(round(step_s * fs))
    if n < 16:
        raise ValueError(f"window length {lw_s} s gives only {n} samples")
    if step < 1:
        raise ValueError("window step rounds to zero samples")
    return n, step


def build_candidates(prep: Prepared, n: int, step: int) -> WindowTable:
    starts, segpos, local = [], [], []
    origin = prep.analyzed_start_index
    for pos, seg in enumerate(prep.segments):
        seg_start = seg.global_start
        seg_stop = seg.global_start + seg.data.shape[1]
        k0 = -(-(seg_start - origin) // step)  # ceil division
        s = origin + k0 * step
        while s + n <= seg_stop:
            starts.append(s)
            segpos.append(pos)
            local.append(s - seg_start)
            s += step
    return WindowTable(
        n_samples=n,
        step_samples=step,
        global_start=np.asarray(starts, dtype=np.int64),
        segment_pos=np.asarray(segpos, dtype=np.int64),
        local_start=np.asarray(local, dtype=np.int64),
    )


def overlaps(a_start: int, b_start: int, n: int) -> bool:
    """Half-open [a, a+n) and [b, b+n) share at least one sample."""
    return a_start < b_start + n and b_start < a_start + n
