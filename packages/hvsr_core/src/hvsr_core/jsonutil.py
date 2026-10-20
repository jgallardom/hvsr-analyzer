"""Canonical JSON, hashing, and null-safe numeric conversion.

Contract (spec 13.3): no NaN/Infinity tokens, no JSON encoded inside strings, unavailable numbers are
``null`` accompanied by a reason elsewhere in the document.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def finite_or_none(value: Any) -> float | None:
    """Convert a scalar to a finite float, or None when missing/non-finite."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def array_or_nulls(values: Iterable[Any]) -> list[float | None]:
    """Convert a 1-D numeric array to a JSON list where non-finite entries become null."""
    arr = np.asarray(values, dtype=float)
    out: list[float | None] = []
    for v in arr.ravel().tolist():
        out.append(v if math.isfinite(v) else None)
    return out


def to_jsonable(obj: Any) -> Any:
    """Recursively convert numpy scalars/arrays, tuples, and sets to JSON-safe values.

    Non-finite floats become None. Raises TypeError for unsupported objects instead of
    stringifying them silently.
    """
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, (int, np.integer)) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return finite_or_none(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind in "fc":
            return [to_jsonable(v) for v in obj.tolist()]
        return to_jsonable(obj.tolist())
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(to_jsonable(v) for v in obj)
    if hasattr(obj, "model_dump"):
        return to_jsonable(obj.model_dump(mode="json"))
    raise TypeError(f"object of type {type(obj).__name__} is not JSON-serializable")


def canonical_dumps(obj: Any) -> str:
    """Deterministic compact JSON: sorted keys, no NaN/Infinity, UTF-8 characters preserved."""
    return json.dumps(
        to_jsonable(obj), sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    )


def pretty_dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, indent=2, allow_nan=False, ensure_ascii=False) + "\n"


def canonical_hash(obj: Any) -> str:
    return sha256_bytes(canonical_dumps(obj).encode("utf-8"))


def strict_loads(text: str | bytes) -> Any:
    """Parse JSON rejecting NaN/Infinity tokens (json.loads accepts them by default)."""

    def _reject(token: str) -> Any:
        raise ValueError(f"non-finite JSON number token {token!r} is not permitted")

    return json.loads(text, parse_constant=_reject)
