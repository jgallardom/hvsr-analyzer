from __future__ import annotations

import functools

import pytest

from hvsr_core.research.synthetic import TFSpec, generate_3c

CASES = {
    "sdof2": TFSpec("sdof", {"fn": 2.0, "zeta": 0.15}),
    "flat": TFSpec("flat"),
    "two_peaks": TFSpec(
        "multi_peak",
        {
            "peaks": [
                {"fc": 1.5, "amplitude": 4.0, "width_decades": 0.06},
                {"fc": 6.0, "amplitude": 3.0, "width_decades": 0.06},
            ]
        },
    ),
    "plateau": TFSpec("lognormal_peak", {"fc": 3.0, "amplitude": 3.0, "width_decades": 0.5}),
}


@functools.lru_cache(maxsize=32)
def record(case: str, seed: int, duration_s: float = 1800.0):
    rec, _ = generate_3c(duration_s=duration_s, fs=50, tf=CASES[case], seed=seed)
    return rec


@pytest.fixture(scope="session")
def make_record():
    return record


def strip_time(obj):
    if isinstance(obj, dict):
        return {k: strip_time(v) for k, v in obj.items() if k != "generated_utc"}
    if isinstance(obj, list):
        return [strip_time(v) for v in obj]
    return obj
