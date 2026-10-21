"""AC-S8-01: the complete upload-to-result flow through the HTTP interface only."""

from __future__ import annotations

import json
import re

import pytest

from hvsr_core.config import AnalysisRequest
from hvsr_web import worker

from stage8_support import CSV_GZ, element_text, fixture_bytes, get_json, submission


def _objects(site) -> set[str]:
    return {i.key for i in site.ctx.store.list("")}


@pytest.mark.acceptance("AC-S8-01")
def test_upload_configure_analyze_and_read_results_over_http(fresh):
    site, c = fresh, fresh.client
    upload = c.get("/upload")
    assert upload.status_code == 200 and 'data-js="upload"' in upload.text
    consent_box = re.search(r'<input type="checkbox" name="consent_checked"[^>]*>', upload.text).group(0)
    assert "checked " not in consent_box.replace('name="consent_checked"', "") and "required" in consent_box

    # 1. validation preview: parses and reports, stores and charges nothing
    before = _objects(site)
    wf = {"csv_layout": "elapsed_time"}
    r = c.post(
        "/api/v1/previews",
        data={"waveform": json.dumps(wf)},
        files=[("files", (CSV_GZ, fixture_bytes(CSV_GZ)))],
        headers=site.auth,
    )
    assert r.status_code == 200, r.text
    prev = r.json()
    assert prev["stored"] is False and prev["summary"]["sampling_rate_hz"] == 50.0
    assert prev["summary"]["duration_s"] == pytest.approx(600.0, abs=0.05)
    assert _objects(site) == before
    assert get_json(site, "/api/v1/measurements")["total"] == 0

    # 2. submission
    r = c.post(
        "/api/v1/submissions",
        data={"submission": json.dumps(submission("flow-submission-01", lat=-12.0464, lon=-77.0428))},
        files=[("files", (CSV_GZ, fixture_bytes(CSV_GZ)))],
        headers=site.auth,
    )
    assert r.status_code == 201, r.text
    sub = r.json()
    code, mid = sub["record_code"], sub["measurement_id"]
    page = c.get(sub["page_url"])
    assert page.status_code == 200
    assert element_text(page.text, "main").startswith(f"{code} (-12.0464, -77.0428)")

    # 3. configuration page -> the request its script would send (defaults as published JSON)
    conf = c.get(f"/measurements/{mid}/configure")
    assert conf.status_code == 200
    defaults = json.loads(
        re.search(r'<script type="application/json" id="request-defaults">(.*?)</script>', conf.text, re.S)
        .group(1)
        .replace("&#34;", '"')
    )
    assert defaults == AnalysisRequest().model_dump(mode="json")
    assert c.post(f"/api/v1/measurements/{mid}/analyses", json=defaults).status_code == 401
    r = c.post(f"/api/v1/measurements/{mid}/analyses", json=defaults, headers=site.auth)
    assert r.status_code == 202, r.text
    job_url = r.json()["job_url"]
    assert get_json(site, job_url)["state"] == "queued"

    # 4. a worker (separate from the web request) executes the durable job
    done = worker.drain(site.ctx)
    assert [d["state"] for d in done] == ["succeeded"]
    job = get_json(site, job_url)
    assert job["state"] == "succeeded" and job["error"] is None
    run_id = job["result"]["run_id"]

    # 5. results: page, figure, downloads, machine-readable run, history, search
    res = c.get(f"/runs/{run_id}")
    assert res.status_code == 200
    assert element_text(res.text, "results-title") == f"{code} (-12.0464, -77.0428)"
    img = re.search(r'<img id="hv-plot" src="([^"]+)"', res.text).group(1)
    fig = c.get(img)
    assert fig.status_code == 200 and fig.headers["content-type"].startswith("image/svg+xml")
    assert fig.content.lstrip().startswith(b"<?xml") and b"<svg" in fig.content
    run = get_json(site, f"/api/v1/runs/{run_id}")
    assert run["record_code"] == code and set(run["methods"]) == {"standard", "sdof"}
    assert run["page_url"] == f"/runs/{run_id}" and run["comparison_url"]
    for method in ("standard", "sdof"):
        kinds = {a["kind"] for a in run["methods"][method]["artifacts"]}
        assert {"analysis_json", "mean_spectrum_csv", "window_table_csv", "plot_png", "plot_svg"} <= kinds
        assert f'href="/runs/{run_id}?method={method}"' in res.text
    history = c.get(f"/measurements/{mid}").text
    assert f'href="/runs/{run_id}"' in history
    found = get_json(site, f"/api/v1/measurements?code={code[:5]}")
    assert found["total"] == 1 and found["items"][0]["record_code"] == code
    assert found["items"][0]["analysis_runs"] == 2  # run A and the planned run B
    assert code in c.get(f"/search?code={code[:5]}").text


@pytest.mark.acceptance("AC-S8-01")
def test_public_search_filters_and_paginates(site, short_run, long_run):
    c = site.client
    everything = get_json(site, "/api/v1/measurements?limit=200")
    codes = {i["record_code"] for i in everything["items"]}
    assert {short_run.record_code, long_run.record_code} <= codes
    madrid = get_json(site, "/api/v1/measurements?lat_min=40&lat_max=41&lon_min=-4&lon_max=-3")
    assert [i["record_code"] for i in madrid["items"]] == [long_run.record_code]
    item = madrid["items"][0]
    assert item["latitude"] == 40.416775 and item["longitude"] == -3.70379
    assert item["page_url"] == f"/measurements/{long_run.measurement_id}"
    assert item["manifest_url"] == f"/api/v1/measurements/{long_run.measurement_id}/manifest"
    first = get_json(site, "/api/v1/measurements?limit=1")
    second = get_json(site, "/api/v1/measurements?limit=1&offset=1")
    assert first["total"] == second["total"] == everything["total"]
    assert first["items"][0]["record_code"] != second["items"][0]["record_code"]
    assert c.get("/api/v1/measurements?limit=201").status_code == 422
    assert get_json(site, "/api/v1/measurements?code=zz%25")["total"] == 0  # LIKE wildcards are literal
    page = c.get("/search?lat_min=40&lat_max=41&lon_min=-4&lon_max=-3")
    assert long_run.record_code in page.text and short_run.record_code not in page.text


def test_pages_send_security_headers_and_never_carry_tokens(site, short_run):
    for url in (
        "/",
        "/upload",
        f"/measurements/{short_run.measurement_id}",
        f"/runs/{short_run.run_id}",
        "/data",
    ):
        r = site.client.get(url, headers=site.auth)
        assert r.status_code == 200
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert site.token not in r.text
        assert not re.search(r"<script(?![^>]*\b(src=|type=\"application/json\"))", r.text), "inline script"
    assert site.client.get(f"/runs/{short_run.run_id}?token=abc").status_code == 400
