"""Stage 6 test helpers (a uniquely named module; `conftest` names collide across stage folders)."""

from __future__ import annotations


def strip_time(obj):
    if isinstance(obj, dict):
        return {k: strip_time(v) for k, v in obj.items() if k != "generated_utc"}
    if isinstance(obj, list):
        return [strip_time(v) for v in obj]
    return obj
