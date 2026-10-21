"""Stage 6: SESAME isolation, replay, cache reuse, report-only changes, quality dimensions."""

from __future__ import annotations

import copy
import math

import numpy as np
import pytest

from hvsr_core import analysis, uncertainty
from hvsr_core.analysis import AnalysisCache, apply_report, run_analysis, verify_replay
from hvsr_core.config import AnalysisConfig, AnalysisRequest, DisplayOptions, ReportOptions
from hvsr_core.core import run_core
from hvsr_core.matching import _bracket, estimate_peaks, match_family
from hvsr_core.schema_validation import validation_errors
from hvsr_core.templates import resolve_sdof_bank

from stage6_support import strip_time

ON = AnalysisRequest(report=ReportOptions(evaluate_sesame=True))
OFF = AnalysisRequest(report=ReportOptions(evaluate_sesame=False))


def _sesame_free(docs: dict) -> dict:
    out = strip_time(copy.deepcopy(docs))
    for d in out.values():
        d.pop("sesame_requested")
        for s in d["selections"]:
            s.pop("sesame")
    return out


@pytest.fixture(scope="module")
def toggled(make_record):
    rec = make_record("sdof2", 5)
    return rec, run_analysis(rec, ON), run_analysis(rec, OFF)


@pytest.mark.acceptance("AC-S6-03")
def test_sesame_toggle_leaves_science_identical(toggled):
    _, on, off = toggled
    assert on.key == off.key and on.run_id == off.run_id
    assert not on.cached and not off.cached  # two independent computations
    assert strip_time(on.scientific) == strip_time(off.scientific)
    assert _sesame_free(on.published) == _sesame_free(off.published)
    for m in ("standard", "sdof"):
        a, b = on.published[m], off.published[m]
        assert a["sesame_requested"] is True and b["sesame_requested"] is False
        for sa, sb in zip(a["selections"], b["selections"], strict=True):
            for k in ("window_ids", "mean_curve", "uncertainty", "peaks", "templates", "window_peaks_hz"):
                assert sa[k] == sb[k], k
            assert sa["sesame"]["status"] == "evaluated"
            assert sb["sesame"] == {"status": "not_requested"}
    assert on.robustness == off.robustness


@pytest.mark.acceptance("AC-S6-03", "AC-S6-05")
def test_published_documents_validate_against_schema(toggled):
    _, on, off = toggled
    for res in (on, off, on.run_b):
        for m, doc in res.published.items():
            assert validation_errors(doc, "analysis") == [], m


@pytest.mark.acceptance("AC-S6-04")
def test_sesame_interval_never_extends_template_scoring(toggled, make_record):
    _, on, _ = toggled
    tpl = on.published["sdof"]["selections"][0]["templates"]
    for item in tpl["selected_in_greedy_order"]:
        assert item["fit_band_hz"] == [item["fp_hz"] / 2, 2 * item["fp_hz"]]
    assert "fit band [fp/2, 2 fp]" in tpl["q_grid"]

    # Perturbing spectra inside the SESAME interval [fp/4, 4 fp] but outside each window's
    # [fp/2, 2 fp] bracket leaves every template score bit-identical.
    cfg = AnalysisConfig()
    core = run_core(make_record("sdof2", 5), cfg)
    sd, fam = core.spectra, next(f for f in core.discovery.families if f.plausible)
    ln_f = np.log(sd.grid.freq)
    rows = np.flatnonzero(core.qc.passed)
    bank, _ = resolve_sdof_bank(cfg.sdof.template_bank, cfg.spectral.ko_bandwidth)
    ln_hv = sd.ln_hv
    peaks = estimate_peaks(
        ln_hv, sd.valid, ln_f, rows, fam.lo_index, fam.hi_index, cfg.sdof.peak_interpolation
    )
    base = match_family(
        ln_hv, sd.valid, ln_f, sd.df_hz, rows, fam.family_id, fam.lo_index, fam.hi_index, bank, cfg, peaks
    )
    pert = ln_hv.copy()
    changed = 0
    for k, r in enumerate(rows):
        if peaks.grid_index[k] < 0:
            continue
        lfp = math.log(peaks.fp[k])
        a, b = _bracket(ln_f, lfp - math.log(2), lfp + math.log(2))
        wide = (ln_f >= lfp - math.log(4)) & (ln_f <= lfp + math.log(4))
        outside = wide.copy()
        outside[a : b + 1] = False
        pert[r, outside] += 3.0
        changed += int(outside.sum())
    assert changed > 0
    out = match_family(
        pert, sd.valid, ln_f, sd.df_hz, rows, fam.family_id, fam.lo_index, fam.hi_index, bank, cfg, peaks
    )
    assert np.array_equal(base.r2, out.r2, equal_nan=True)
    assert np.array_equal(base.best_k, out.best_k) and base.status == out.status


@pytest.mark.acceptance("AC-S6-05")
def test_replay_bypasses_cache_and_reproduces(make_record, monkeypatch):
    rec = make_record("sdof2", 5)
    cache = AnalysisCache()
    res = run_analysis(rec, ON, cache=cache)
    calls = []
    real = analysis.compute_science
    monkeypatch.setattr(analysis, "compute_science", lambda *a, **k: calls.append(1) or real(*a, **k))
    monkeypatch.setattr(AnalysisCache, "get", lambda self, key: pytest.fail("replay must not read the cache"))
    rep = verify_replay(rec, ON, res.scientific)
    assert calls == [1] and rep["recomputed"] is True
    assert rep["status"] == "reproduced" and rep["n_differences"] == 0, rep["differences"]
    assert rep["environment_differences"] == []
    assert rep["run_id"] == res.run_id and rep["tolerance_policy"]["float_rtol"] == 1e-9

    tampered = copy.deepcopy(res.scientific)
    curve = tampered["sdof"]["selections"][0]["mean_curve"]["hv_geometric_mean"]
    i = next(j for j, v in enumerate(curve) if v is not None)
    curve[i] *= 1 + 1e-6
    sel = tampered["standard"]["selections"][0]
    sel["window_ids"] = sel["window_ids"][::-1]
    tampered["standard"]["provenance"]["git_commit"] = "0" * 40
    bad = verify_replay(rec, ON, tampered)
    paths = [d["path"] for d in bad["differences"]]
    assert bad["status"] == "differs"
    assert any("hv_geometric_mean" in p for p in paths) and any("window_ids" in p for p in paths)
    assert any("git_commit" in d["path"] for d in bad["environment_differences"])


@pytest.mark.acceptance("AC-S6-08")
def test_report_and_display_changes_do_not_recompute(make_record, monkeypatch):
    rec = make_record("sdof2", 5)
    cache = AnalysisCache()
    first = run_analysis(rec, ON, cache=cache)

    def boom(*a, **k):
        raise AssertionError("scientific computation invoked by a report/display-only change")

    monkeypatch.setattr(np.fft, "rfft", boom)
    monkeypatch.setattr(uncertainty, "replicate_counts", boom)
    monkeypatch.setattr(analysis, "compute_science", boom)
    cosmetic = AnalysisRequest(
        report=ReportOptions(evaluate_sesame=False),
        display=DisplayOptions(show_individual_windows=True, dpi=300, figure_width_in=12),
    )
    res = run_analysis(rec, cosmetic, cache=cache)
    assert res.cached and res.run_b.cached
    assert res.scientific is first.scientific
    assert all(
        s["sesame"]["status"] == "not_requested" for d in res.published.values() for s in d["selections"]
    )
    url = f"/api/v1/comparisons/{first.robustness['comparison_id']}"
    again = apply_report(first.scientific, ReportOptions(evaluate_sesame=True), first.robustness, url)
    assert again == first.published


@pytest.mark.acceptance("AC-S6-09")
def test_quality_dimensions_are_separate_without_summary_score(toggled, make_record):
    _, on, _ = toggled
    flat = run_analysis(make_record("flat", 3), ON)
    for res in (on, flat):
        for doc in res.published.values():
            q = doc["quality_dimensions"]
            assert set(q) == {
                "usability",
                "peak_identifiability",
                "sensitivity",
                "summary_score",
                "summary_score_reason",
            }
            assert q["summary_score"] is None and "D-061" in q["summary_score_reason"]

            def keys(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        yield k
                        yield from keys(v)
                elif isinstance(o, list):
                    for v in o:
                        yield from keys(v)

            for dim in ("usability", "peak_identifiability", "sensitivity"):
                assert not [k for k in keys(q[dim]) if any(w in k for w in ("score", "grade", "rating"))]
            assert q["sensitivity"]["robustness"]["status"] == res.robustness["status"]
    # a flat curve is not penalized: usability carries the same fields, with no quality verdict
    assert set(flat.published["standard"]["quality_dimensions"]["usability"]) == set(
        on.published["standard"]["quality_dimensions"]["usability"]
    )


@pytest.mark.acceptance("AC-S6-11")
def test_flat_record_has_no_forced_peak_and_unchecked_is_not_requested(make_record):
    rec = make_record("flat", 3)
    on, off = run_analysis(rec, ON), run_analysis(rec, OFF)
    std = on.published["standard"]
    assert not [f for f in std["peak_families"] if f["plausible"]]
    for s in std["selections"]:
        rep = s["sesame"]
        assert rep["status"] == "not_evaluable"
        assert rep["not_evaluable_checks"] == ["R1", "R2", "R3", "C1", "C2", "C3", "C4", "C5", "C6"]
        assert "peaks" not in rep
    for d in off.published.values():
        assert all(s["sesame"] == {"status": "not_requested"} for s in d["selections"])


@pytest.mark.acceptance("AC-S6-12")
def test_identical_requests_reuse_immutable_cached_result(make_record, tmp_path, monkeypatch):
    rec = make_record("sdof2", 5)
    cache = AnalysisCache(tmp_path)
    calls = []
    real = analysis.compute_science
    monkeypatch.setattr(analysis, "compute_science", lambda *a, **k: calls.append(1) or real(*a, **k))
    r1 = run_analysis(rec, ON, cache=cache)
    assert len(calls) == 2 and not r1.cached  # runs A and B
    r2 = run_analysis(rec, ON, cache=cache)
    assert len(calls) == 2 and r2.cached and r2.scientific is r1.scientific
    r3 = run_analysis(rec, AnalysisRequest(display=DisplayOptions(dpi=72)), cache=cache)
    assert len(calls) == 2 and r3.cached
    # persisted across cache instances, loaded from JSON only
    fresh = AnalysisCache(tmp_path)
    r4 = run_analysis(rec, ON, cache=fresh)
    assert len(calls) == 2 and r4.cached and strip_time(r4.scientific) == strip_time(r1.scientific)
    # first result wins: a later put cannot replace the stored document
    fresh.put(r1.key, {"standard": {"run_id": "tampered"}})
    assert fresh.get(r1.key)["standard"]["run_id"] == r1.run_id
    assert AnalysisCache(tmp_path).get(r1.key)["standard"]["run_id"] == r1.run_id
    # the same bytes under another measurement or metadata snapshot are a different run
    other = run_analysis(rec, ON, analysis.RecordInfo(measurement_id="m-2", metadata_revision=1), cache=cache)
    assert not other.cached and other.run_id != r1.run_id
    assert other.published["standard"]["record"]["measurement_id"] == "m-2"
    assert len(calls) == 4
    # a scientific change misses the cache
    base = AnalysisConfig()
    cfg = base.model_copy(update={"windowing": base.windowing.model_copy(update={"step_s": 10.0})})
    r5 = run_analysis(rec, AnalysisRequest(config=cfg, robustness_check=False), cache=cache)
    assert not r5.cached and r5.key != r1.key and len(calls) == 5
