"""A valid recording whose SDOF method has no plausible peak family still publishes a complete run:
the analysis states the status and reason, no figure is invented, and every page and link works."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hvsr_core.config import AnalysisRequest
from hvsr_core.exports import validate_csv
from hvsr_core.jsonutil import strict_loads
from hvsr_web import analysis_jobs, worker
from hvsr_web.api import create_app
from hvsr_web.db import Artifact

from stage9_support import make_ctx, new_contributor, submit_synthetic


@pytest.mark.acceptance("AC-S8-01")
def test_a_method_without_selections_publishes_and_renders(tmp_path):
    ctx = make_ctx(tmp_path)
    token, c = new_contributor(ctx)
    sub = submit_synthetic(ctx, c, 600, seed=11)  # deterministic: no plausible peak family
    out = analysis_jobs.request_analysis(
        ctx, sub.measurement_id, AnalysisRequest(robustness_check=False), c.id
    )
    [res] = worker.drain(ctx)
    assert res["job_id"] == out["job_id"] and res["state"] == "succeeded", res
    run_id = res["result"]["run_id"]
    with ctx.sessions() as session:
        arts = {
            (a.kind, a.method): a for a in session.scalars(select(Artifact).where(Artifact.run_id == run_id))
        }
    sdof = strict_loads(ctx.store.get(arts[("analysis_json", "sdof")].blob_key))
    assert sdof["method_status"] == "not_computable" and sdof["selections"] == []
    assert "no plausible peak family" in sdof["method_status_reason"]
    assert not any(k.startswith("plot") and m == "sdof" for k, m in arts)  # no invented figure
    assert any(k.startswith("plot") and m == "standard" for k, m in arts)
    mean_csv = ctx.store.get(arts[("mean_spectrum_csv", "sdof")].blob_key)
    assert validate_csv(mean_csv, "mean_spectrum") == []

    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    page = client.get(f"/runs/{run_id}?method=sdof")
    assert page.status_code == 200
    assert 'id="method-status"' in page.text and "not_computable" in page.text and "hv-plot" not in page.text
    assert client.get(f"/runs/{run_id}?method=standard").status_code == 200
    assert client.get(f"/measurements/{sub.measurement_id}").status_code == 200
    r = client.post(
        f"/api/v1/runs/{run_id}/plots",
        json={"method": "sdof", "format": "svg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404 and "no selection" in r.text
    manifest = client.get(f"/api/v1/measurements/{sub.measurement_id}/manifest").json()
    for entries in manifest["categories"].values():
        for e in entries:
            if e.get("url") and e.get("availability") == "available":
                assert client.get(e["url"], follow_redirects=True).status_code == 200, e["url"]
