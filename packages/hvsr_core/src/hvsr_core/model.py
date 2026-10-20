"""In-memory canonical numerical recording.

Conventions (docs/units-and-timing.md):
- Times are UTC. Absolute instants are integer nanoseconds since the Unix epoch (``*_utc_ns``)
  and serialize as ISO 8601 strings with a trailing ``Z``.
- Sample intervals are half-open ``[start, stop)`` integer indices. Time bounds are derived from
  integer indices (``t = t0 + index / fs``), never from cumulative floating-point addition.
- A recording is a list of contiguous, synchronized segments. Gaps separate segments; samples are
  never interpolated across gaps.
- Component order inside arrays is always (N, E, Z). Original labels are preserved per channel.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

Quantity = Literal["counts", "velocity", "acceleration", "displacement", "other", "unknown"]
COMPONENTS: tuple[str, str, str] = ("N", "E", "Z")


def ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    sec, rem = divmod(int(ns), 1_000_000_000)
    base = _dt.datetime.fromtimestamp(sec, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    if rem == 0:
        return base + "Z"
    frac = f"{rem:09d}".rstrip("0")
    return f"{base}.{frac}Z"


def iso_to_ns(text: str) -> int:
    """Parse an ISO 8601 timestamp WITH explicit offset (Z or +HH:MM) to epoch nanoseconds.

    Sub-microsecond digits are honoured exactly. Strings without an explicit offset are rejected:
    the caller must declare a time zone explicitly instead of us inferring one.
    """
    s = text.strip()
    if not s:
        raise ValueError("empty timestamp")
    offset_ns = 0
    if s.endswith("Z") or s.endswith("z"):
        body = s[:-1]
    else:
        # find the offset sign after the time part
        tpos = s.find("T") if "T" in s else s.find(" ")
        if tpos < 0:
            raise ValueError(f"timestamp {text!r} has no time component")
        tail = s[tpos:]
        sign_pos = max(tail.rfind("+"), tail.rfind("-"))
        if sign_pos <= 0:
            raise ValueError(f"timestamp {text!r} has no explicit UTC offset; declare the time zone explicitly")
        off = tail[sign_pos:]
        body = s[: tpos + sign_pos]
        sign = 1 if off[0] == "+" else -1
        hhmm = off[1:].replace(":", "")
        if len(hhmm) != 4 or not hhmm.isdigit():
            raise ValueError(f"invalid UTC offset in {text!r}")
        offset_ns = sign * (int(hhmm[:2]) * 3600 + int(hhmm[2:]) * 60) * 1_000_000_000
    body = body.replace(" ", "T")
    if "." in body:
        main, frac = body.split(".", 1)
        if not frac.isdigit() or len(frac) > 9:
            raise ValueError(f"invalid fractional seconds in {text!r} (max 9 digits)")
        frac_ns = int(frac.ljust(9, "0"))
    else:
        main, frac_ns = body, 0
    base = _dt.datetime.strptime(main, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.UTC)
    epoch_s = int(base.timestamp())
    return epoch_s * 1_000_000_000 + frac_ns - offset_ns


@dataclass(frozen=True)
class ChannelMeta:
    component: Literal["N", "E", "Z"]
    source_label: str
    label_kind: Literal["NEZ", "12Z_mapped"] = "NEZ"
    azimuth_deg: float | None = None
    dip_deg: float | None = None
    quantity: Quantity = "unknown"
    units: str | None = None
    sensitivity: float | None = None
    sensitivity_units: str | None = None
    full_scale: float | None = None
    sensed_quantity: Literal["velocity", "acceleration", "displacement", "other", "unknown"] = "unknown"

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "source_label": self.source_label,
            "label_kind": self.label_kind,
            "azimuth_deg": self.azimuth_deg,
            "dip_deg": self.dip_deg,
            "quantity": self.quantity,
            "sensed_quantity": self.sensed_quantity,
            "units": self.units,
            "sensitivity": self.sensitivity,
            "sensitivity_units": self.sensitivity_units,
            "full_scale": self.full_scale,
        }


@dataclass
class Segment:
    """Contiguous synchronized samples for N, E, Z (shape (3, n))."""

    start_index: int
    start_offset_s: float
    data: np.ndarray

    @property
    def n(self) -> int:
        return int(self.data.shape[1])

    @property
    def stop_index(self) -> int:
        return self.start_index + self.n


@dataclass
class Gap:
    after_index: int
    gap_s: float
    kind: Literal["gap", "overlap"] = "gap"


@dataclass
class RecordingDiagnostics:
    gaps: list[Gap] = field(default_factory=list)
    timing_irregular: bool = False
    max_timing_jitter_s: float | None = None
    timing_policy: str = "strict"
    channel_start_offsets_s: dict[str, float] = field(default_factory=dict)
    clipped_samples: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class Recording:
    sampling_rate_hz: float
    t0_utc_ns: int | None
    t0_source: Literal["data", "declared", "unknown"]
    channels: tuple[ChannelMeta, ChannelMeta, ChannelMeta]
    segments: list[Segment]
    sampling_rate_source: Literal["data", "declared", "timestamps"] = "data"
    sample_dtype: Literal["float64", "int32"] = "float64"
    diagnostics: RecordingDiagnostics = field(default_factory=RecordingDiagnostics)
    source: dict = field(default_factory=dict)

    @property
    def dt_s(self) -> float:
        return 1.0 / self.sampling_rate_hz

    @property
    def total_samples(self) -> int:
        return sum(s.n for s in self.segments)

    @property
    def span_samples(self) -> int:
        if not self.segments:
            return 0
        return self.segments[-1].stop_index - self.segments[0].start_index

    @property
    def duration_s(self) -> float:
        return self.span_samples / self.sampling_rate_hz

    def time_of_index_s(self, index: int) -> float:
        return index / self.sampling_rate_hz

    def end_utc_ns(self) -> int | None:
        if self.t0_utc_ns is None:
            return None
        return self.t0_utc_ns + round(self.span_samples * 1e9 / self.sampling_rate_hz)

    def summary(self) -> dict:
        """Pre-analysis summary shown before any analysis is run (spec 2.1)."""
        d = self.diagnostics
        return {
            "sampling_rate_hz": self.sampling_rate_hz,
            "sampling_rate_source": self.sampling_rate_source,
            "components": [c.as_dict() for c in self.channels],
            "start_utc": ns_to_iso(self.t0_utc_ns),
            "start_time_source": self.t0_source,
            "end_utc": ns_to_iso(self.end_utc_ns()),
            "duration_s": self.duration_s,
            "sample_count_per_channel": self.total_samples,
            "segment_count": len(self.segments),
            "units": {c.component: c.units for c in self.channels},
            "quantities": {c.component: c.quantity for c in self.channels},
            "gaps": [{"after_index": g.after_index, "gap_s": g.gap_s, "kind": g.kind} for g in d.gaps],
            "timing_irregular": d.timing_irregular,
            "max_timing_jitter_s": d.max_timing_jitter_s,
            "timing_policy": d.timing_policy,
            "channel_start_offsets_s": d.channel_start_offsets_s,
            "clipped_samples": d.clipped_samples,
            "sample_dtype": self.sample_dtype,
            "warnings": list(d.warnings),
            "errors": list(d.errors),
        }


class InputError(ValueError):
    """Raised for invalid, ambiguous, or unsupported input. Message is user-facing."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
