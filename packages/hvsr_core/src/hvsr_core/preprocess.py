"""Record-level preprocessing (prep-v1) and analyzed-span selection.

Order (docs/scientific-decisions.md D-010): validated canonical segments -> analyzed span (integer
sample bounds) -> optional scalar sensitivity correction -> optional Butterworth filter (off by
default). Per-window operations (demean, detrend, taper) happen in spectra.py. Every operation that
alters samples is returned as a derived operation record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import signal

from .config import AnalysisConfig
from .model import InputError, Recording


@dataclass
class PreparedSegment:
    segment_index: int
    global_start: int
    data: np.ndarray  # (3, n) float64, N/E/Z


@dataclass
class Prepared:
    fs: float
    segments: list[PreparedSegment]
    analyzed_start_index: int
    analyzed_stop_index: int
    derived_operations: list[dict]
    warnings: list[str]


def analyzed_bounds(rec: Recording, config: AnalysisConfig) -> tuple[int, int]:
    fs = rec.sampling_rate_hz
    tr = config.windowing.time_range
    first = rec.segments[0].start_index
    last = rec.segments[-1].stop_index
    start = first if tr.tmin_s is None else first + math.ceil(tr.tmin_s * fs - 1e-9)
    stop = last if tr.tmax_s is None else min(last, first + math.floor(tr.tmax_s * fs + 1e-9))
    if stop <= start:
        raise InputError("empty_time_range", "the analyzed time range contains no samples")
    return start, stop


def prepare(rec: Recording, config: AnalysisConfig, instrument: dict) -> Prepared:
    if rec.diagnostics.errors:
        raise InputError("recording_has_errors", "; ".join(rec.diagnostics.errors))
    fs = rec.sampling_rate_hz
    start, stop = analyzed_bounds(rec, config)
    ops: list[dict] = []
    warnings: list[str] = []
    scale = np.array([instrument["scale"][c] for c in ("N", "E", "Z")], dtype=float)[:, None]
    if instrument.get("correction") == "scalar_sensitivity":
        ops.append(
            {"operation": "scalar_sensitivity_correction", "parameters": {"multipliers": instrument["scale"]}}
        )
    sos = None
    bp = config.preprocessing.bandpass
    if bp is not None:
        nyq = fs / 2
        lo, hi = bp.low_hz, bp.high_hz
        if hi is not None and hi >= nyq:
            raise InputError("filter_nyquist", f"low-pass corner {hi} Hz must be below Nyquist {nyq} Hz")
        if lo is not None and hi is not None:
            sos = signal.butter(bp.order, [lo, hi], btype="bandpass", fs=fs, output="sos")
        elif lo is not None:
            sos = signal.butter(bp.order, lo, btype="highpass", fs=fs, output="sos")
        else:
            sos = signal.butter(bp.order, hi, btype="lowpass", fs=fs, output="sos")
        ops.append(
            {
                "operation": "butterworth_filter",
                "parameters": bp.model_dump(mode="json")
                | {"implementation": "scipy.signal.sosfiltfilt" if bp.zero_phase else "scipy.signal.sosfilt"},
            }
        )
    segs: list[PreparedSegment] = []
    for si, seg in enumerate(rec.segments):
        a, b = max(seg.start_index, start), min(seg.stop_index, stop)
        if b <= a:
            continue
        data = np.asarray(seg.data[:, a - seg.start_index : b - seg.start_index], dtype=np.float64)
        if instrument.get("correction") == "scalar_sensitivity":
            data = data * scale
        if sos is not None:
            data = (
                signal.sosfiltfilt(sos, data, axis=1) if bp.zero_phase else signal.sosfilt(sos, data, axis=1)
            )
        segs.append(PreparedSegment(si, a, np.ascontiguousarray(data)))
    lw = config.windowing.window_length_s
    if config.peak_search.fmin_hz <= 10.0 / lw:
        warnings.append(
            f"peak search starts at {config.peak_search.fmin_hz:g} Hz <= 10/Lw = {10.0 / lw:g} Hz; peaks there cannot "
            "pass SESAME reliability criterion (i) (strict inequality). Longer windows are recommended for low frequencies."
        )
    return Prepared(fs, segs, start, stop, ops, warnings)
