"""Mutation check: the CRITICAL no-alignment test must fail for aligned or normalized final curves."""

from __future__ import annotations

import numpy as np
import pytest
import test_matching

import hvsr_core.matching as matching
from hvsr_core.curves import MeanCurve, geometric_mean_curve


def _aligned(sd, rows):
    mc = geometric_mean_curve(sd, rows)
    pk = np.array([int(np.nanargmax(np.where(sd.valid[r], sd.ln_hv[r], -np.inf))) for r in rows])
    ref = int(np.median(pk))
    shifted = np.vstack([np.roll(sd.ln_hv[r], ref - p) for r, p in zip(rows, pk, strict=True)])
    ln = np.nanmean(shifted, axis=0)
    return MeanCurve(np.exp(ln), ln, mc.log_std, mc.count, mc.valid, mc.n_windows)


def _normalized(sd, rows):
    mc = geometric_mean_curve(sd, rows)
    top = np.nanmax(mc.ln_mean)
    return MeanCurve(np.exp(mc.ln_mean - top), mc.ln_mean - top, mc.log_std, mc.count, mc.valid, mc.n_windows)


@pytest.mark.acceptance("AC-S3-14")
@pytest.mark.parametrize("mutant", [_aligned, _normalized])
def test_critical_test_kills_alignment_mutants(monkeypatch, mutant):
    monkeypatch.setattr(matching, "geometric_mean_curve", mutant)
    with pytest.raises(AssertionError):
        test_matching.test_CRITICAL_final_mean_uses_original_unaligned_spectra()
