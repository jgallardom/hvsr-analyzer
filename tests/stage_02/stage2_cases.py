from __future__ import annotations

import functools

import pytest

from hvsr_core.config import AnalysisConfig
from hvsr_core.core import run_core
from hvsr_core.research.synthetic import TFSpec, generate_3c

CASES = {
    "flat": TFSpec("flat"),
    "sdof2": TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}),
    "broad3": TFSpec("lognormal_peak", {"fc": 3.0, "amplitude": 3.0, "width_decades": 0.2}),
    "two_peaks": TFSpec(
        "multi_peak",
        {"peaks": [{"fc": 1.5, "amplitude": 4.0, "width_decades": 0.06}, {"fc": 6.0, "amplitude": 4.0, "width_decades": 0.06}]},
    ),
    "weak_low": TFSpec(
        "multi_peak",
        {"peaks": [{"fc": 1.0, "amplitude": 2.2, "width_decades": 0.06}, {"fc": 6.0, "amplitude": 6.0, "width_decades": 0.06}]},
    ),
    "low_edge": TFSpec("lognormal_peak", {"fc": 0.12, "amplitude": 4.0, "width_decades": 0.15}),
}
SEEDS = (1, 2, 3)


@functools.lru_cache(maxsize=64)
def synthetic(case: str, seed: int, **kw):
    return generate_3c(duration_s=1200, fs=50, tf=CASES[case], seed=seed, **kw)


@functools.lru_cache(maxsize=64)
def core_run(case: str, seed: int):
    rec, truth = synthetic(case, seed)
    return run_core(rec, AnalysisConfig()), truth


@pytest.fixture(scope="session")
def run_case():
    return core_run
