"""Stage 6: predeclared 50 s / 75 s robustness comparison (robust-lw1.5-v1, D-060)."""

from __future__ import annotations

import copy

import pytest

from hvsr_core.analysis import run_analysis
from hvsr_core.config import AnalysisConfig, AnalysisRequest
from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_core.robustness import CRITERIA, compare_runs

SEEDS = (1, 2, 3, 4, 5)


def _run(rec, config=None):
    return run_analysis(rec, AnalysisRequest(config=config or AnalysisConfig()))


@pytest.fixture(scope="module")
def clean_runs(make_record):
    return [_run(make_record("sdof2", s)) for s in SEEDS]


@pytest.fixture(scope="module")
def plateau_runs(make_record):
    return [_run(make_record("plateau", s)) for s in SEEDS]


@pytest.mark.acceptance("AC-S6-06")
def test_criteria_are_the_predeclared_d060_values():
    assert CRITERIA == {
        "version": "robust-lw1.5-v1",
        "family_match_max_abs_log10_ratio": 0.1,
        "fp_max_abs_ln_ratio": 0.05,
        "ap_max_abs_ln_ratio": 0.20,
        "curve_max_median_abs_ln_diff": 0.10,
        "resolved_min_cycles_per_window": 10.0,
        "resolved_max_fft_resolution_over_fp": 0.10,
    }


@pytest.mark.acceptance("AC-S6-06")
def test_clean_sdof_runs_agree_within_tolerances(clean_runs):
    for res in clean_runs:
        c = res.robustness
        assert c["window_length_a_s"] == 50.0 and c["window_length_b_s"] == 75.0
        assert res.run_b is not None and res.run_b.run_id == c["run_b"] != res.run_id
        assert c["status"] == "consistent", c["methods"]
        for m in ("standard", "sdof"):
            v = c["methods"][m]
            assert v["status"] == "consistent" and not v["issues"] and not v["unmatched_run_b_families"]
            for p in v["peaks"]:
                assert p["matched"] and p["adequately_resolved"]
                assert abs(p["fp_ln_ratio"]) <= CRITERIA["fp_max_abs_ln_ratio"]
                assert abs(p["ap_ln_ratio"]) <= CRITERIA["ap_max_abs_ln_ratio"]
                assert p["curve"]["median_abs_ln_diff"] <= CRITERIA["curve_max_median_abs_ln_diff"]
                assert p["fft_resolution_a_hz"] == pytest.approx(1 / 50) and p[
                    "fft_resolution_b_hz"
                ] == pytest.approx(1 / 75)
                assert (
                    p["half_amplitude_width_ln_a"] is not None and p["half_amplitude_width_ln_b"] is not None
                )
                assert 0 < p["selected_time_a_also_selected_in_b"] <= 1
        assert any("correlated" in n for n in c["notes"])
        assert any("not an equivalence test" in n for n in c["notes"])


@pytest.mark.acceptance("AC-S6-07")
def test_broad_plateau_is_flagged_not_forced_to_agree(plateau_runs, clean_runs):
    """A 0.5-decade flat-topped peak has a poorly constrained fp; the comparison must expose it."""
    flagged = [r for r in plateau_runs if r.robustness["status"] == "sensitive"]
    assert flagged, [r.robustness["status"] for r in plateau_runs]
    assert len(flagged) > sum(r.robustness["status"] == "sensitive" for r in clean_runs)
    for res in flagged:
        c = res.robustness
        for v in c["methods"].values():
            if v["status"] != "sensitive":
                continue
            assert v["issues"]
            for p in v["peaks"]:
                assert "fp_a_hz" in p and (not p["matched"] or "fp_b_hz" in p)
        # both runs are kept separately and linked; nothing is averaged or chosen
        assert res.run_b.published["standard"]["recording"]["window_length_s"] == 75.0
        assert (
            res.published["standard"]["quality_dimensions"]["sensitivity"]["robustness"]["status"]
            == "sensitive"
        )
        assert res.published["standard"]["robustness_comparison_url"].endswith(c["comparison_id"])
        assert not {"mean", "average", "combined", "preferred"} & set(c)


@pytest.mark.acceptance("AC-S6-07")
def test_multi_peak_families_are_compared_separately(make_record):
    res = _run(make_record("two_peaks", 11))
    for m in ("standard", "sdof"):
        v = res.robustness["methods"][m]
        fams = {p["family_a"] for p in v["peaks"]}
        assert len(fams) == len(v["peaks"]) >= 2  # one row per plausible family, none merged
        fps = sorted(p["fp_a_hz"] for p in v["peaks"])
        assert fps[0] == pytest.approx(1.5, rel=0.1) and fps[-1] == pytest.approx(6.0, rel=0.1)


@pytest.mark.acceptance("AC-S6-07")
def test_insufficient_run_b_is_not_evaluable_without_alternative():
    base = AnalysisConfig()
    cfg = base.model_copy(update={"windowing": base.windowing.model_copy(update={"window_length_s": 20.0})})
    tf = TFSpec("sdof", {"fn": 4.0, "zeta": 0.15})
    # 110 s: A has 5 non-overlapping 20 s windows, B only 3 of 30 s (< 5 required)
    rec, _ = generate_3c(duration_s=110, fs=50, tf=tf, seed=2)
    res = _run(rec, cfg)
    c = res.robustness
    assert res.published["standard"]["method_status"] == "computed"
    assert c["status"] == "not_evaluable" and c["window_length_b_s"] == 30.0
    assert all(v["status"] == "not_evaluable" and "run B" in v["reason"] for v in c["methods"].values())
    # 25 s: no 30 s window fits at all; the planned B is reported, no other B is tried
    rec, _ = generate_3c(duration_s=25, fs=50, tf=tf, seed=2)
    c = _run(rec, cfg).robustness
    assert c["status"] == "not_evaluable" and c["run_b"] is None and c["window_length_b_s"] == 30.0
    assert any("no 30.0 s window fits" in n for n in c["notes"])


@pytest.mark.acceptance("AC-S6-07")
def test_comparison_logic_on_modified_run_b(clean_runs):
    res = clean_runs[0]
    docs_a, docs_b = res.scientific, res.run_b.scientific

    def shifted(factor):
        d = copy.deepcopy(docs_b)
        for doc in d.values():
            for s in doc["selections"]:
                for p in s["peaks"]:
                    if p.get("observed_peak_frequency_hz"):
                        p["observed_peak_frequency_hz"] *= factor
        return d

    assert compare_runs(docs_a, shifted(1.04))["methods"]["standard"]["status"] == "consistent"
    s = compare_runs(docs_a, shifted(1.08))["methods"]["standard"]
    assert s["status"] == "sensitive" and any("fp differs" in i for i in s["issues"])

    missing = copy.deepcopy(docs_b)
    for doc in missing.values():
        for f in doc["peak_families"]:
            f["plausible"] = False
    s = compare_runs(docs_a, missing)["methods"]["standard"]
    assert s["status"] == "sensitive" and "plausible peak families differ between runs" in s["issues"]

    unresolved = copy.deepcopy(docs_a)
    for doc in unresolved.values():
        doc["recording"]["window_length_s"] = 4.0  # fp * Lw < 10 cycles
    s = compare_runs(unresolved, docs_b)["methods"]["standard"]
    assert s["status"] == "not_evaluable" and not any(p["adequately_resolved"] for p in s["peaks"])

    none_b = compare_runs(docs_a, None, "run B failed", 75.0)
    assert none_b["status"] == "not_evaluable" and none_b["window_length_b_s"] == 75.0
