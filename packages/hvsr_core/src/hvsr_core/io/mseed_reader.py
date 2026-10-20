"""MiniSEED reader built on ObsPy (tested-supported subset only).

Supported (declared after tests in tests/stage_01): MiniSEED 2.x records with encodings INT16,
INT32, FLOAT32, FLOAT64, STEIM1, STEIM2 as read by the pinned ObsPy version. MiniSEED 3 is rejected
because the pinned ObsPy (1.5.1) has no MiniSEED 3 reader. Other MiniSEED 2 encodings (ASCII,
GEOSCOPE, SRO, ...) are rejected because they are not tested here.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

import numpy as np

from ..model import ChannelMeta, Gap, InputError, Recording, RecordingDiagnostics, Segment
from .limits import InputLimits

SUPPORTED_ENCODINGS = ("INT16", "INT32", "FLOAT32", "FLOAT64", "STEIM1", "STEIM2")
INTEGER_ENCODINGS = ("INT16", "INT32", "STEIM1", "STEIM2")


@dataclass
class ChannelSegment:
    start_ns: int
    fs: float
    data: np.ndarray


def detect_miniseed_version(data: bytes) -> int:
    if len(data) >= 3 and data[:2] == b"MS" and data[2] == 3:
        return 3
    if len(data) >= 8:
        seq = data[:6]
        if (
            all(ch in b"0123456789 \x00" for ch in seq)
            and data[6:7] in (b"D", b"R", b"Q", b"M")
            and data[7:8] in (b" ", b"\x00")
        ):
            return 2
    return 0


def _read_stream(data: bytes, name: str, limits: InputLimits):
    from obspy import read

    version = detect_miniseed_version(data)
    if version == 3:
        raise InputError(
            "unsupported_miniseed_version",
            f"{name}: MiniSEED 3 is not supported by the pinned reader (ObsPy 1.5.1)",
        )
    if version != 2:
        raise InputError("not_miniseed", f"{name}: not a recognisable MiniSEED 2 file")
    try:
        head = read(io.BytesIO(data), format="MSEED", headonly=True)
    except Exception as exc:  # noqa: BLE001 - ObsPy raises many types for corrupt input
        raise InputError(
            "miniseed_corrupt", f"{name}: unreadable MiniSEED ({type(exc).__name__}: {exc})"
        ) from exc
    for tr in head:
        if tr.stats.npts > limits.max_samples_per_channel:
            raise InputError(
                "sample_limit",
                f"{name}: trace {tr.id} has more than {limits.max_samples_per_channel} samples",
            )
        enc = tr.stats.mseed.encoding
        if enc not in SUPPORTED_ENCODINGS:
            raise InputError(
                "unsupported_encoding",
                f"{name}: encoding {enc} of {tr.id} is not in the tested-supported list {SUPPORTED_ENCODINGS}",
            )
    total = sum(tr.stats.npts for tr in head)
    if total * 8 > limits.max_decoded_bytes:
        raise InputError("decoded_size_limit", f"{name}: decoded samples exceed the configured limit")
    try:
        st = read(io.BytesIO(data), format="MSEED")
    except Exception as exc:  # noqa: BLE001
        raise InputError(
            "miniseed_corrupt", f"{name}: unreadable MiniSEED ({type(exc).__name__}: {exc})"
        ) from exc
    return st


def read_miniseed(
    files: list[tuple[str, bytes]],
    mapping: dict[str, str] | None = None,
    limits: InputLimits | None = None,
    alignment_tolerance_fraction: float = 0.1,
) -> Recording:
    """Read one or more MiniSEED files into a synchronized three-component Recording.

    mapping: {"N": "NET.STA.LOC.CHA", "E": ..., "Z": ...}; required when channels are 1/2/Z, when
    more than three channels are present, or when station/location codes differ.
    """
    limits = limits or InputLimits()
    if len(files) > limits.max_files:
        raise InputError("file_limit", f"more than {limits.max_files} waveform files")
    by_id: dict[str, list] = {}
    encodings: dict[str, set] = {}
    for name, data in files:
        if len(data) > limits.max_request_bytes:
            raise InputError("request_size_limit", f"{name} exceeds the per-request byte limit")
        st = _read_stream(data, name, limits)
        for tr in st:
            by_id.setdefault(tr.id, []).append(tr)
            encodings.setdefault(tr.id, set()).add(tr.stats.mseed.encoding)
    if len(by_id) > limits.max_channels:
        raise InputError("channel_limit", f"more than {limits.max_channels} channels")
    ids = sorted(by_id)
    if mapping:
        if set(mapping) != {"N", "E", "Z"}:
            raise InputError("channel_mapping", "mapping must assign exactly N, E and Z")
        missing = [v for v in mapping.values() if v not in by_id]
        if missing:
            raise InputError("channel_mapping", f"mapped channels not found: {missing}; available: {ids}")
        if len(set(mapping.values())) != 3:
            raise InputError("channel_mapping", "mapping assigns one channel to several components")
        chosen = dict(mapping)
        kinds = {c: ("12Z_mapped" if chosen[c].split(".")[-1][-1:] in "12" else "NEZ") for c in chosen}
    else:
        chosen, kinds = _auto_map(ids)

    per_comp: dict[str, list[ChannelSegment]] = {}
    diag = RecordingDiagnostics()
    fs_set = set()
    for comp in ("N", "E", "Z"):
        traces = sorted(by_id[chosen[comp]], key=lambda t: t.stats.starttime.ns)
        segs = []
        for tr in traces:
            fs = float(tr.stats.sampling_rate)
            fs_set.add(fs)
            segs.append(ChannelSegment(int(tr.stats.starttime.ns), fs, np.asarray(tr.data)))
        per_comp[comp] = _merge_contiguous(segs, comp, diag)
    if len(fs_set) != 1:
        raise InputError(
            "sampling_rate_mismatch",
            f"components have different sampling rates {sorted(fs_set)}; channels are never resampled",
        )
    fs = fs_set.pop()
    rec = _intersect(per_comp, fs, alignment_tolerance_fraction, diag)
    enc_all = set().union(*(encodings[chosen[c]] for c in ("N", "E", "Z")))
    int_data = enc_all <= set(INTEGER_ENCODINGS)
    chans = []
    for comp in ("N", "E", "Z"):
        chans.append(ChannelMeta(component=comp, source_label=chosen[comp], label_kind=kinds[comp]))  # type: ignore[arg-type]
    rec.channels = tuple(chans)  # type: ignore[assignment]
    rec.sample_dtype = "int32" if int_data else "float64"
    rec.source = {
        "format": "miniseed2",
        "files": [n for n, _ in files],
        "channel_ids": chosen,
        "encodings": {c: sorted(encodings[chosen[c]]) for c in ("N", "E", "Z")},
    }
    if any(k == "12Z_mapped" for k in kinds.values()):
        diag.warnings.append(
            "horizontal channels 1/2 were mapped manually to N/E; RMS combination is rotation-invariant, geometric is not"
        )
    if rec.duration_s > limits.max_duration_s:
        raise InputError("duration_limit", f"recording longer than {limits.max_duration_s} s")
    return rec


def _auto_map(ids: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    if len(ids) != 3:
        raise InputError(
            "channel_mapping_required", f"found {len(ids)} channels {ids}; provide an explicit N/E/Z mapping"
        )
    prefixes = {i.rsplit(".", 1)[0] + "." + i.rsplit(".", 1)[1][:2] for i in ids}
    if len(prefixes) != 1:
        raise InputError(
            "channel_mapping_required",
            f"channels {ids} do not share network/station/location/band; provide a mapping",
        )
    last = {i.rsplit(".", 1)[1][-1:]: i for i in ids}
    if set(last) == {"N", "E", "Z"}:
        return {c: last[c] for c in ("N", "E", "Z")}, {c: "NEZ" for c in ("N", "E", "Z")}
    if set(last) == {"1", "2", "Z"}:
        raise InputError(
            "components_require_mapping",
            f"channels {ids} use 1/2 horizontals; provide an explicit mapping with orientation",
        )
    raise InputError("channel_mapping_required", f"cannot identify N/E/Z among {ids}; provide a mapping")


def _merge_contiguous(
    segs: list[ChannelSegment], comp: str, diag: RecordingDiagnostics
) -> list[ChannelSegment]:
    out: list[ChannelSegment] = []
    for seg in segs:
        if not out:
            out.append(seg)
            continue
        prev = out[-1]
        dt_ns = 1e9 / prev.fs
        expected = prev.start_ns + prev.data.size * dt_ns
        delta = seg.start_ns - expected
        if delta < -0.5 * dt_ns:
            raise InputError(
                "overlapping_data",
                f"component {comp}: overlapping or duplicated records ({-delta / 1e9:.6f} s overlap)",
            )
        if abs(delta) <= 0.5 * dt_ns and seg.fs == prev.fs:
            out[-1] = ChannelSegment(prev.start_ns, prev.fs, np.concatenate([prev.data, seg.data]))
        else:
            out.append(seg)
    return out


def _intersect(
    per_comp: dict[str, list[ChannelSegment]], fs: float, tol_frac: float, diag: RecordingDiagnostics
) -> Recording:
    dt_ns = 1e9 / fs
    ref0 = min(s.start_ns for segs in per_comp.values() for s in segs)
    # Express each channel segment as [start_idx, stop_idx) on the reference grid; check alignment.
    idx_segs: dict[str, list[tuple[int, int, ChannelSegment]]] = {}
    for comp, segs in per_comp.items():
        lst = []
        for s in segs:
            exact = (s.start_ns - ref0) / dt_ns
            i0 = int(round(exact))
            frac = abs(exact - i0)
            if frac > tol_frac:
                raise InputError(
                    "channels_not_synchronized",
                    f"component {comp} samples are offset by {frac:.3f} samples from the other channels; channels are never resampled",
                )
            diag.channel_start_offsets_s[comp] = max(diag.channel_start_offsets_s.get(comp, 0.0), frac / fs)
            lst.append((i0, i0 + s.data.size, s))
        idx_segs[comp] = lst

    # Intersection of the three interval sets.
    def intervals(comp: str) -> list[tuple[int, int]]:
        return [(a, b) for a, b, _ in idx_segs[comp]]

    common = intervals("N")
    for comp in ("E", "Z"):
        nxt = []
        for a, b in common:
            for c, d in intervals(comp):
                lo, hi = max(a, c), min(b, d)
                if hi > lo:
                    nxt.append((lo, hi))
        common = sorted(nxt)
    if not common:
        raise InputError("no_common_data", "the three components have no overlapping samples")
    first = common[0][0]
    segments = []
    for lo, hi in common:
        arrs = []
        for comp in ("N", "E", "Z"):
            for a, b, s in idx_segs[comp]:
                if a <= lo and hi <= b:
                    arrs.append(np.asarray(s.data[lo - a : hi - a], dtype=np.float64))
                    break
        segments.append(Segment(lo - first, (lo - first) / fs, np.vstack(arrs)))
    for prev, nxt in zip(segments[:-1], segments[1:], strict=True):
        diag.gaps.append(Gap(after_index=prev.stop_index - 1, gap_s=(nxt.start_index - prev.stop_index) / fs))
    if diag.gaps:
        diag.warnings.append(
            f"{len(diag.gaps)} gap(s) detected; windows never span gaps and gaps are not interpolated"
        )
    t0 = ref0 + int(round(first * dt_ns))
    if not math.isfinite(fs) or fs <= 0:
        raise InputError("sampling_rate", "invalid sampling rate")
    return Recording(
        sampling_rate_hz=fs,
        t0_utc_ns=t0,
        t0_source="data",
        channels=(ChannelMeta("N", "N"), ChannelMeta("E", "E"), ChannelMeta("Z", "Z")),
        segments=segments,
        sampling_rate_source="data",
        diagnostics=diag,
    )
