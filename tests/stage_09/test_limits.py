"""Per-job process isolation: CPU, memory, and wall-time limits, durable retries, thread pinning,
and the concurrency ceiling, exercised with real child processes."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from threadpoolctl import threadpool_info

from hvsr_core.config import AnalysisRequest
from hvsr_web import analysis_jobs, api, isolation, jobs, worker
from hvsr_web.api import create_app
from hvsr_web.db import AnalysisRun, Job
from hvsr_web.submissions import WaveformOptions

from stage9_handlers import REF
from stage9_support import make_ctx, new_contributor, submission, submit_synthetic, synthetic_csv_gz


def _job(ctx, job_id) -> Job:
    with ctx.sessions() as session:
        return session.get(Job, job_id)


def _run(ctx, kind, payload=None, key=None):
    job = jobs.enqueue(ctx, kind, payload or {}, key or f"{kind}-{time.monotonic_ns()}")
    t0 = time.monotonic()
    out = isolation.run_isolated_once(ctx, "stage9-worker", REF)
    return job.id, out, time.monotonic() - t0


@pytest.mark.acceptance("AC-S9-04")
def test_cpu_time_limit_kills_the_job_and_records_a_permanent_failure(tmp_path):
    ctx = make_ctx(tmp_path, job_cpu_time_s=1, job_wall_time_s=120)
    job_id, out, elapsed = _run(ctx, "spin")
    assert out["limit"] == "cpu_time" and out["state"] == "dead"
    j = _job(ctx, job_id)
    assert j.state == "dead" and j.attempts == 1 and "CPU-time limit exceeded" in j.last_error
    assert elapsed < 60


@pytest.mark.acceptance("AC-S9-04")
def test_wall_time_limit_kills_a_stalled_job(tmp_path):
    ctx = make_ctx(tmp_path, job_wall_time_s=2, job_cpu_time_s=600)
    job_id, out, elapsed = _run(ctx, "sleep", {"s": 120})
    assert out["limit"] == "wall_time" and out["state"] == "dead"
    j = _job(ctx, job_id)
    assert j.state == "dead" and "wall-time limit exceeded" in j.last_error and j.lease_token is None
    assert elapsed < 60


@pytest.mark.acceptance("AC-S9-04")
def test_memory_limit_is_a_clear_permanent_failure(tmp_path):
    ctx = make_ctx(tmp_path, job_memory_bytes=256 * 1024 * 1024)
    job_id, out, _ = _run(ctx, "alloc", {"bytes": 4 * 1024**3})
    assert out["limit"] == "memory" and out["state"] == "dead"
    assert "memory limit exceeded" in _job(ctx, job_id).last_error
    # the same handler within the limit succeeds: the limit, not the handler, caused the failure
    _, ok, _ = _run(ctx, "alloc", {"bytes": 16 * 1024 * 1024})
    assert ok["state"] == "succeeded" and ok["result"]["n"] == 2 * 1024 * 1024
    assert ok["process"]["limits"]["address_space_bytes"] > 256 * 1024 * 1024


@pytest.mark.acceptance("AC-S9-04")
def test_crashed_job_processes_are_retried_durably_then_abandoned(tmp_path):
    ctx = make_ctx(tmp_path, job_max_attempts=2)
    marker = tmp_path / "crashed-once"
    flaky_id, first, _ = _run(ctx, "crash_once", {"marker": str(marker)})
    assert first["state"] == "queued" and first["exitcode"] == 9  # retryable: the job row survived
    assert _job(ctx, flaky_id).state == "queued"
    second = isolation.run_isolated_once(ctx, "another-worker", REF)
    assert second["job_id"] == flaky_id and second["state"] == "succeeded"
    assert _job(ctx, flaky_id).result == {"recovered": True}

    bad_id, out1, _ = _run(ctx, "crash")
    assert out1["state"] == "queued"
    out2 = isolation.run_isolated_once(ctx, "stage9-worker", REF)
    assert out2["job_id"] == bad_id and out2["state"] == "dead"
    j = _job(ctx, bad_id)
    assert j.attempts == 2 and "ended abnormally (exit code 3)" in j.last_error
    assert isolation.run_isolated_once(ctx, "stage9-worker", REF) is None


@pytest.mark.acceptance("AC-S9-04")
def test_a_real_analysis_runs_in_an_isolated_process_and_publishes(tmp_path):
    ctx = make_ctx(tmp_path)
    _, c = new_contributor(ctx)
    r = submit_synthetic(ctx, c, 600, seed=5)
    out = analysis_jobs.request_analysis(ctx, r.measurement_id, AnalysisRequest(), c.id)
    [res] = worker.drain(ctx, isolated=True)
    assert res["job_id"] == out["job_id"] and res["state"] == "succeeded"
    assert res["process"]["peak_rss_bytes"] > 0 and res["process"]["cpu_s"] > 0
    with ctx.sessions() as session:
        runs = session.query(AnalysisRun).filter_by(measurement_id=r.measurement_id).all()
    assert {x.role for x in runs} == {"primary", "robustness_b"}
    assert _job(ctx, out["job_id"]).state == "succeeded"


@pytest.mark.acceptance("AC-S9-12")
def test_job_processes_pin_numerical_threads(tmp_path):
    for n in (1, 2):
        ctx = make_ctx(tmp_path, f"threads{n}", numeric_threads=n)
        _, out, _ = _run(ctx, "threads")
        assert out["state"] == "succeeded"
        res = out["result"]
        assert res["pools"], "no numerical thread pool was loaded"
        assert all(p["num_threads"] == n for p in res["pools"]), res["pools"]
        assert set(res["env"].values()) == {str(n)}
    assert res["cpu_count"] > 2  # pinning matters: the host has more cores than the pool uses


@pytest.mark.acceptance("AC-S9-12")
def test_web_process_pins_numerical_threads(tmp_path):
    ctx = make_ctx(tmp_path, numeric_threads=1)
    create_app(ctx)
    assert all(p["num_threads"] == 1 for p in threadpool_info())


@pytest.mark.acceptance("AC-S9-12")
def test_uploads_are_parsed_off_the_event_loop_within_a_slot_limit(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path, max_concurrent_uploads=1, upload_wait_s=0.5)
    token, _ = new_contributor(ctx)
    entered, release = threading.Event(), threading.Event()
    real_submit = api.submit

    def held_submit(*args, **kwargs):
        entered.set()
        assert release.wait(60)
        return real_submit(*args, **kwargs)

    monkeypatch.setattr(api, "submit", held_submit)
    data = synthetic_csv_gz(120, seed=91)

    def post(client, path, key):
        form = (
            {"submission": submission(key).model_dump_json()}
            if path.endswith("submissions")
            else {"waveform": WaveformOptions(csv_layout="elapsed_time").model_dump_json()}
        )
        return client.post(
            path, headers={"Authorization": f"Bearer {token}"}, data=form,
            files=[("files", ("rec.csv.gz", data))],
        )  # fmt: skip

    with TestClient(create_app(ctx)) as client:
        first: dict = {}
        holder = threading.Thread(
            target=lambda: first.update(r=post(client, "/api/v1/submissions", "upload-slot-1"))
        )
        holder.start()
        assert entered.wait(60)
        t0 = time.monotonic()
        assert client.get("/healthz").status_code == 200  # the event loop is not blocked by the parse
        assert time.monotonic() - t0 < 5
        for path in ("/api/v1/submissions", "/api/v1/previews"):
            busy = post(client, path, "upload-slot-2")
            assert busy.status_code == 503 and busy.headers["retry-after"] == "10"
            assert busy.json()["detail"]["error"] == "busy"
        release.set()
        holder.join(60)
        assert first["r"].status_code == 201, first["r"].text
        assert post(client, "/api/v1/previews", "upload-slot-3").status_code == 200
        assert post(client, "/api/v1/submissions", "upload-slot-2").status_code == 201


@pytest.mark.acceptance("AC-S9-12")
def test_concurrent_workers_respect_the_job_ceiling(tmp_path):
    ctx = make_ctx(tmp_path, max_concurrent_jobs=1)
    for i in range(2):
        jobs.enqueue(ctx, "sleep", {"s": 4}, f"sleep-{i}")
    results: list = [None, None]

    def run(i):
        results[i] = isolation.run_isolated_once(ctx, f"worker-{i}", REF)

    first = threading.Thread(target=run, args=(0,))
    first.start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with ctx.sessions() as session:
            if session.query(Job).filter_by(state="running").count():
                break
        time.sleep(0.05)
    run(1)  # a second worker while the first job runs: refused by the ceiling
    first.join()
    assert results[0]["state"] == "succeeded" and results[1] is None
    with ctx.sessions() as session:
        states = sorted(j.state for j in session.query(Job))
    assert states == ["queued", "succeeded"]
