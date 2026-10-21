"""Warm-cache requests reuse stored science: a new report option, new figure options, a cosmetic
re-render, and every page or download of an existing run run no spectra, selection, or bootstrap."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import hvsr_core.analysis
from hvsr_core.config import AnalysisRequest
from hvsr_web import analysis_jobs, worker
from hvsr_web.api import create_app
from hvsr_web.db import AnalysisRun, Artifact

from stage9_support import make_ctx, new_contributor, submit_synthetic


@pytest.mark.acceptance("AC-S9-05")
def test_warm_cache_requests_do_not_rerun_science(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    token, c = new_contributor(ctx)
    auth = {"Authorization": f"Bearer {token}"}
    sub = submit_synthetic(ctx, c, 600, seed=5)
    cold = analysis_jobs.request_analysis(ctx, sub.measurement_id, AnalysisRequest(), c.id)
    [cold_res] = worker.drain(ctx)
    assert cold_res["job_id"] == cold["job_id"] and cold_res["result"]["science_cached"] is False
    run_id = cold_res["result"]["run_id"]

    calls: list = []

    def no_science(*args, **kwargs):
        calls.append(args)
        raise AssertionError("science was recomputed on a warm cache")

    monkeypatch.setattr(hvsr_core.analysis, "compute_science", no_science)

    warm_requests = [
        AnalysisRequest.model_validate({"report": {"evaluate_sesame": False}}),
        AnalysisRequest.model_validate({"display": {"figure_width_in": 7.0, "dpi": 120}}),
    ]
    for req in warm_requests:
        out = analysis_jobs.request_analysis(ctx, sub.measurement_id, req, c.id)
        assert out["job_id"] != cold["job_id"]
        [res] = worker.drain(ctx)
        assert res["job_id"] == out["job_id"] and res["state"] == "succeeded", res
        assert res["result"]["science_cached"] is True and res["result"]["run_id"] == run_id
    with ctx.sessions() as session:
        runs = session.scalars(
            select(AnalysisRun).where(AnalysisRun.measurement_id == sub.measurement_id)
        ).all()
        analyses = session.scalars(
            select(Artifact).where(Artifact.run_id == run_id, Artifact.kind == "analysis_json")
        ).all()
    assert sorted(r.role for r in runs) == ["primary", "robustness_b"]  # no new science, no new runs
    tags = {a.generated_by["report_options"]["evaluate_sesame"] for a in analyses}
    assert tags == {True, False}  # the report-only variant was published alongside

    client = TestClient(create_app(ctx))
    assert client.get(f"/runs/{run_id}").status_code == 200
    assert client.get(f"/runs/{run_id}?method=sdof&alt=true").status_code == 200
    r = client.post(
        f"/api/v1/runs/{run_id}/plots",
        json={"method": "standard", "format": "png", "display": {"dpi": 100}},
        headers=auth,
    )
    assert r.status_code == 201
    manifest = client.get(f"/api/v1/measurements/{sub.measurement_id}/manifest").json()
    served = 0
    for entries in manifest["categories"].values():
        for e in entries:
            if e.get("availability") == "available" and e.get("url"):
                assert client.get(e["url"], follow_redirects=True).status_code == 200
                served += 1
    assert served > 10 and calls == []
