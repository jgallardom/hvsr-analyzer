"""Resource-safety limits for decoding untrusted input (spec 3.2).

These are operational safety ceilings, independent of the contributor's monthly quota. Defaults are
initial examples to benchmark (docs/performance.md), configurable through environment variables in
the web app.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

from ..model import InputError

MiB = 1024 * 1024


@dataclass(frozen=True)
class InputLimits:
    max_request_bytes: int = 128 * MiB
    max_decoded_bytes: int = 768 * MiB
    max_decompression_ratio: float = 200.0
    ratio_check_min_output_bytes: int = 8 * MiB
    max_samples_per_channel: int = 8_640_000
    max_channels: int = 12
    max_files: int = 6
    max_duration_s: float = 172_800.0


def is_gzip(data: bytes) -> bool:
    return len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B


def safe_gunzip(data: bytes, limits: InputLimits) -> bytes:
    """Decompress (possibly multi-member) gzip with decoded-size and ratio ceilings."""
    out = bytearray()
    remaining = data
    consumed_total = 0
    while remaining:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        buf = remaining
        while buf:
            chunk = d.decompress(buf, 1 * MiB)
            out += chunk
            consumed = len(buf) - len(d.unconsumed_tail)
            consumed_total += consumed
            buf = d.unconsumed_tail
            _check(len(out), consumed_total, limits)
            if d.eof:
                break
        if not d.eof:
            # flush any buffered output
            out += d.flush()
            _check(len(out), consumed_total, limits)
            if not d.eof:
                raise InputError("truncated_gzip", "gzip stream is truncated or corrupt")
        remaining = d.unused_data
        while remaining[:1] == b"\x00":  # tolerate zero padding between/after members
            remaining = remaining[1:]
    return bytes(out)


def _check(decoded: int, consumed: int, limits: InputLimits) -> None:
    if decoded > limits.max_decoded_bytes:
        raise InputError(
            "decoded_size_limit",
            f"decompressed size exceeds the configured limit of {limits.max_decoded_bytes} bytes",
        )
    if decoded >= limits.ratio_check_min_output_bytes and consumed > 0 and decoded / consumed > limits.max_decompression_ratio:
        raise InputError(
            "decompression_ratio_limit",
            f"decompression ratio exceeds {limits.max_decompression_ratio:g}:1 (possible decompression bomb)",
        )
