"""Stage 2: shape-neutral peak discovery, families, interpretation statuses, source diagnostics.

Synthetic truth: horizontals carry |T(f)| over a common base spectrum, so expected RMS H/V = |T|.
Tolerance on frequencies (8 %) covers Konno-Ohmachi b = 40 smoothing (+/- 3.5 % half-power width of
the smoothing window at the peak), per-window estimator scatter over ~24 non-overlapping 50 s windows,
and grid quantization (0.5 % per point). It is not tuned per case; all cases share it.
"""

from __future__ import annotations

import numpy as np
import pytest

from hvsr_core.config import AnalysisConfig
from hvsr_core.core import run_core
from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_core.windows import overlaps

from stage2_cases import SEEDS, core_run

TOL = 0.08


def _near(a, b, tol=TOL):
    return a is not None and abs(a / b - 1) <= tol


@pytest.mark.acceptance("AC-S2-06")
@pytest.mark.parametrize("seed", SEEDS)
def test_flat_record_has_no_clear_peak(seed):
    run, _ = core_run("flat", seed)
    d = run.discovery
    assert d.status == "no_clear_peak"
    assert d.fundamental_hz is None and d.fundamental_family_id is None
    assert not d.plausible_families()
    # a numerical global maximum still exists and is stored separately
    assert d.global_max_hz is not None and d.global_max_amplitude is not None


@pytest.mark.acceptance("AC-S2-07")
@pytest.mark.parametrize("seed", SEEDS)
def test_clean_single_resonance(seed):
    run, truth = core_run("sdof2", seed)
    d = run.discovery
    assert d.status == "single_plausible_peak"
    assert _near(d.fundamental_hz, truth["tf_peak_frequency_hz"])
    fam = d.plausible_families()[0]
    assert fam.lo_hz <= truth["tf_peak_frequency_hz"] <= fam.hi_hz
    assert fam.recurrence >= 0.9


@pytest.mark.acceptance("AC-S2-08")
@pytest.mark.parametrize("seed", SEEDS)
def test_broad_and_multiple_peaks_become_families(seed):
    broad, truth = core_run("broad3", seed)
    pb = broad.discovery.plausible_families()
    assert broad.discovery.status == "single_plausible_peak" and len(pb) == 1
    assert _near(pb[0].mean_peak_hz, truth["tf_peak_frequency_hz"])
    two, _ = core_run("two_peaks", seed)
    d = two.discovery
    assert d.status == "multiple_plausible_peaks"
    centers = sorted(f.mean_peak_hz for f in d.plausible_families())
    assert len(centers) == 2 and _near(centers[0], 1.5) and _near(centers[1], 6.0)
    ids = [f.family_id for f in d.families]
    assert ids == [f"F{i}" for i in range(1, len(ids) + 1)]  # ordered by frequency, stable IDs
    for a, b in zip(d.families, d.families[1:], strict=False):
        assert a.hi_index < b.lo_index  # disjoint families


@pytest.mark.acceptance("AC-S2-09")
@pytest.mark.parametrize("seed", SEEDS)
def test_weak_low_peak_not_overwritten_by_global_maximum(seed):
    run, _ = core_run("weak_low", seed)
    d = run.discovery
    assert _near(d.global_max_hz, 6.0)  # numerical maximum kept separately
    assert d.status == "multiple_plausible_peaks"
    assert _near(d.fundamental_hz, 1.0)  # lowest plausible family, not the maximum
    low = next(f for f in d.families if f.family_id == d.fundamental_family_id)
    assert low.plausible and low.mean_peak_amplitude < d.global_max_amplitude


@pytest.mark.acceptance("AC-S2-13")
def test_discovery_uses_nonoverlapping_windows_and_overlap_cannot_inflate_recurrence():
    run, _ = core_run("sdof2", 3)
    rows = run.discovery.rows
    gs = run.window.global_start[rows]
    for i in range(rows.size):
        for j in range(i + 1, rows.size):
            assert not overlaps(int(gs[i]), int(gs[j]), run.window.n_samples)
    assert run.window.count > 5 * rows.size  # many overlapping candidates were available
    for fam in run.discovery.families:
        assert fam.windows_with_peak <= rows.size
        assert fam.recurrence == pytest.approx(fam.windows_with_peak / rows.size)
    # identical discovery when overlapping candidates are removed (step = Lw)
    rec, _ = generate_3c(duration_s=1200, fs=50, tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}), seed=3)
    alt = run_core(rec, AnalysisConfig.model_validate({"windowing": {"step_s": 50}}))
    assert alt.window.count == rows.size
    a = [(f.center_hz, f.recurrence, f.plausible) for f in run.discovery.families]
    b = [(f.center_hz, f.recurrence, f.plausible) for f in alt.discovery.families]
    assert a == b


@pytest.mark.acceptance("AC-S2-16")
@pytest.mark.parametrize("seed", SEEDS)
def test_persistent_narrow_line_flagged_as_possible_source_not_removed(seed):
    rec, truth = generate_3c(
        duration_s=1200,
        fs=50,
        tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}),
        seed=seed,
        tones=[{"freq_hz": 7.3, "amplitude_rel": 1.5, "components": "NE"}],
    )
    d = run_core(rec, AnalysisConfig()).discovery
    tone = [f for f in d.families if f.lo_hz <= 7.3 <= f.hi_hz]
    assert tone and tone[0].plausible and "possible_source_peak" in tone[0].flags
    ev = tone[0].evidence["source_diagnostic"]
    assert ev["line_width_bins"] <= 3 and ev["persistence"] >= 0.8 and ev["components_above_contrast"] >= 2
    assert ev["line_frequency_hz"] == pytest.approx(7.3, abs=0.02)
    assert "possible_source_peak" in d.flags
    # the resonance is not flagged and remains the fundamental candidate
    res = [f for f in d.families if f.lo_hz <= truth["tf_peak_frequency_hz"] <= f.hi_hz][0]
    assert res.plausible and "possible_source_peak" not in res.flags
    assert res.evidence["source_diagnostic"]["possible_source_peak"] is False
    assert d.status == "single_plausible_peak" and _near(d.fundamental_hz, truth["tf_peak_frequency_hz"])


@pytest.mark.acceptance("AC-S2-16")
def test_only_a_source_line_gives_possible_source_status():
    rec, _ = generate_3c(
        duration_s=1200, fs=50, tf=TFSpec("flat"), seed=4, tones=[{"freq_hz": 7.3, "amplitude_rel": 1.5, "components": "NE"}]
    )
    d = run_core(rec, AnalysisConfig()).discovery
    assert d.status == "possible_source_peak" and d.fundamental_hz is None


@pytest.mark.acceptance("AC-S2-17")
@pytest.mark.parametrize("seed", SEEDS)
def test_lower_band_unresolved_when_rising_at_lowest_usable_frequency(seed):
    run, _ = core_run("low_edge", seed)
    d = run.discovery
    assert d.status == "lower_band_unresolved"
    assert "lower_band_unresolved" in d.flags
    assert d.fundamental_hz is None
    assert d.lower_band["unresolved"] and d.lower_band["lowest_usable_frequency_hz"] == pytest.approx(0.2, rel=0.02)
    # a resolved low peak inside coverage is not flagged
    ok, _ = core_run("sdof2", seed)
    assert not ok.discovery.lower_band["unresolved"]


def test_non_free_field_context_gives_no_site_frequency_candidate():
    rec, _ = generate_3c(duration_s=1200, fs=50, tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}), seed=1)
    d = run_core(rec, AnalysisConfig(), context="building_upper_floor").discovery
    assert d.status == "single_plausible_peak" and d.fundamental_hz is None
    assert "non_free_field_context" in d.flags


def test_too_few_windows_is_not_computable():
    rec, _ = generate_3c(duration_s=200, fs=50, tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}), seed=1)
    d = run_core(rec, AnalysisConfig()).discovery
    assert d.status == "not_computable" and d.fundamental_hz is None
    assert np.isfinite(d.mean.hv).any()
