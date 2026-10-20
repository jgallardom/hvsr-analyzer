"""Null and power checks behind the D-050 revision-2 dependence rules (see docs/scientific-decisions.md)."""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from hvsr_core.config import Uncertainty
from hvsr_core.uncertainty import dependence_test, plan_original_time

CFG = Uncertainty()


def _ar1_rows(n: int, p: int, phi: float, rng: np.random.Generator) -> np.ndarray:
    x = np.empty((n, p))
    x[0] = rng.standard_normal(p) / np.sqrt(1 - phi**2)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.standard_normal(p)
    return x


def test_raw_threshold_alone_would_double_often_under_independence():
    """Motivation for the significance bound: r > 0.3 alone fires in a large share of iid cases."""
    rng = np.random.default_rng(0)
    hits = 0
    for _ in range(2000):
        x = rng.standard_normal((12, 4))
        hits += max(np.corrcoef(x[:-1, j], x[1:, j])[0, 1] for j in range(4)) > 0.3
    assert hits / 2000 > 0.2


@pytest.mark.parametrize("pairs", [5, 11, 23, 40])
def test_false_doubling_rate_is_controlled_under_independence(pairs):
    rng = np.random.default_rng(pairs)
    reps = 2000
    hits = 0
    for _ in range(reps):
        t = dependence_test(rng.standard_normal((pairs + 1, 4)), np.arange(pairs), CFG)
        hits += bool(t["dependent"])
    assert hits / reps <= 0.035, hits / reps


@pytest.mark.acceptance("AC-S4-04")
def test_strong_dependence_is_detected_and_lengthens_blocks():
    rng = np.random.default_rng(2)
    detected = 0
    for _ in range(200):
        F = _ar1_rows(48, 3, 0.95, rng)
        plan = plan_original_time(np.arange(48) * 50.0, 0.0, 50.0, F, CFG)
        detected += plan.diagnostics["doublings"] >= 1
    assert detected / 200 >= 0.8, detected / 200


def test_iid_windows_rarely_lose_estimability_at_typical_record_length():
    """24 independent windows (20-min record, Lw = 50 s) should normally keep 12 blocks."""
    rng = np.random.default_rng(3)
    lost = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for _ in range(500):
            plan = plan_original_time(np.arange(24) * 50.0, 0.0, 50.0, rng.standard_normal((24, 4)), CFG)
            lost += plan.n_blocks < CFG.min_informative_blocks
    assert lost / 500 <= 0.05, lost / 500
