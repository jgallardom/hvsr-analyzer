"""Stage 6: attributed, append-only human annotations that never overwrite computed values."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from hvsr_core.analysis import run_analysis
from hvsr_core.annotations import Annotation, AnnotationLog, AnnotationTarget, merge_for_display
from hvsr_core.config import AnalysisRequest
from hvsr_core.jsonutil import canonical_hash


@pytest.fixture(scope="module")
def doc(make_record):
    return run_analysis(make_record("sdof2", 5), AnalysisRequest(robustness_check=False)).published[
        "standard"
    ]


@pytest.mark.acceptance("AC-S6-10")
def test_annotations_are_separate_and_attributed(doc):
    before = copy.deepcopy(doc)
    log = AnnotationLog()
    tgt = AnnotationTarget(run_id=doc["run_id"], method="standard", peak_family_id="F3")
    a = log.add(tgt, "interpretation", "Site f0 judged at 1.8 Hz from borehole data.", "J. Analyst", 1.8)
    view = merge_for_display(doc, log.for_run(doc["run_id"]))
    assert doc == before  # computed document untouched
    assert view["computed"] == before and view["computed"] is not doc
    assert view["computed_hash"] == canonical_hash(before)
    (ann,) = view["annotations"]
    assert ann["source"] == "human" and ann["attribution"] == "J. Analyst" and ann["current"]
    assert ann["interpreted_f0_hz"] == 1.8
    peak = view["computed"]["selections"][0]["peaks"][0]["observed_peak_frequency_hz"]
    assert peak == before["selections"][0]["peaks"][0]["observed_peak_frequency_hz"] != 1.8
    assert a.annotation_id.startswith("a")


@pytest.mark.acceptance("AC-S6-10")
def test_annotations_cannot_carry_or_change_computed_fields(doc):
    tgt = AnnotationTarget(run_id=doc["run_id"])
    log = AnnotationLog()
    a = log.add(tgt, "comment", "check", "Reviewer")
    with pytest.raises(ValidationError):
        Annotation(**a.model_dump(), observed_peak_frequency_hz=9.9)
    with pytest.raises(ValidationError):
        AnnotationTarget(run_id=doc["run_id"], mean_curve=[1, 2])
    with pytest.raises(ValidationError):
        a.text = "edited"  # frozen
    with pytest.raises(ValidationError):
        log.add(tgt, "verdict", "x", "Reviewer")  # unknown kind
    with pytest.raises(ValidationError):
        log.add(tgt, "comment", "x", "")  # attribution required
    assert not hasattr(log, "remove") and not hasattr(log, "update") and not hasattr(log, "__setitem__")


@pytest.mark.acceptance("AC-S6-10")
def test_superseding_is_append_only(doc):
    log = AnnotationLog()
    tgt = AnnotationTarget(run_id=doc["run_id"], method="sdof")
    first = log.add(tgt, "interpretation", "f0 about 2 Hz", "A", 2.0)
    second = log.add(
        tgt, "interpretation", "revised: f0 about 1.9 Hz", "A", 1.9, supersedes=first.annotation_id
    )
    other = log.add(AnnotationTarget(run_id="r-other"), "comment", "elsewhere", "B")
    assert len(log) == 3
    view = merge_for_display(doc, log.for_run(doc["run_id"]))
    ids = [(x["annotation_id"], x["current"]) for x in view["annotations"]]
    assert ids == [(first.annotation_id, False), (second.annotation_id, True)]
    assert other.annotation_id not in {x["annotation_id"] for x in view["annotations"]}
    with pytest.raises(KeyError):
        log.add(tgt, "comment", "x", "A", supersedes="a-unknown")
