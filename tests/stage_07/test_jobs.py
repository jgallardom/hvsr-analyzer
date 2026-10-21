"""Durable jobs, compute budgets, and crash-safe publication of analysis runs."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hvsr_core.config import AnalysisConfig, AnalysisRequest
from hvsr_core.jsonutil import strict_loads
from hvsr_core.metadata import MeasurementMetadata
from hvsr_core.schema_validation import validation_errors
from hvsr_web import analysis_jobs, jobs
from hvsr_web.api import create_app
from hvsr_web.context import AppContext
from hvsr_web.db import AnalysisRun, Artifact, ComputeUsage, Job, Measurement
from hvsr_web.manifests import build_manifest
from hvsr_web.records import correct_metadata
from hvsr_web.submissions import submit

from stage7_support import artifacts, csv_gz_files, new_contributor, request


def _submit(ctx, key="job-000001"):
    _, c = new_contributor(ctx)
    return c, submit(ctx, c, request(key), csv_gz_files())


def _counts(ctx) -> tuple[int, int, int]:
    with ctx.sessions() as session:
        return (
            session.scalar(select(func.count()).select_from(AnalysisRun)),
            session.scalar(select(func.count()).select_from(Artifact).where(Artifact.run_id.is_not(None))),
            session.scalar(select(func.count()).select_from(ComputeUsage)),
        )


def _job(ctx, job_id) -> Job:
    with ctx.sessions() as session:
        return session.get(Job, job_id)


def _doc(ctx, a: Artifact) -> dict:
    return strict_loads(ctx.store.get(a.blob_key))


# ------------------------------------------------------------------------------------- job table
@pytest.mark.acceptance("AC-S7-19", "AC-S7-15")
def test_claims_are_atomic_and_bounded_by_the_concurrency_ceiling(make_ctx):
    ctx = make_ctx("claims", max_concurrent_jobs=3)
    ids = {jobs.enqueue(ctx, "noop", {"i": i}, f"noop-{i}").id for i in range(5)}
    assert len(ids) == 5 and jobs.enqueue(ctx, "noop", {"i": 0}, "noop-0").id in ids  # idempotent enqueue
    barrier, claims, lock = threading.Barrier(8), [], threading.Lock()

    def worker(i):
        barrier.wait()
        got = jobs.claim(ctx, f"worker-{i}")
        with lock:
            claims.append(got)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = [c for c in claims if c is not None]
    assert len(got) == 3 and len({c.job_id for c in got}) == 3 and len({c.token for c in got}) == 3
    assert jobs.claim(ctx, "late") is None  # ceiling reached
    assert jobs.complete(ctx, got[0], {"ok": True})
    assert jobs.claim(ctx, "late") is not None and jobs.claim(ctx, "later") is None


@pytest.mark.acceptance("AC-S7-19")
def test_expired_lease_is_recovered_and_the_old_holder_is_fenced(ctx, clock):
    job = jobs.enqueue(ctx, "noop", {}, "lease-0001")
    a = jobs.claim(ctx, "worker-a")
    assert jobs.claim(ctx, "worker-b") is None and jobs.heartbeat(ctx, a)
    clock.advance(ctx.settings.job_lease_s + 1)
    b = jobs.claim(ctx, "worker-b")
    assert b.job_id == a.job_id and b.token != a.token and b.attempt == 2
    assert not jobs.heartbeat(ctx, a)
    assert not jobs.complete(ctx, a, {"by": "a"}) and jobs.fail(ctx, a, "late failure") is None
    assert jobs.complete(ctx, b, {"by": "b"})
    j = _job(ctx, job.id)
    assert j.state == "succeeded" and j.result == {"by": "b"} and j.lease_token is None


@pytest.mark.acceptance("AC-S7-19")
def test_retries_are_bounded_and_permanent_errors_are_not_retried(ctx, clock):
    attempts = []

    def flaky(_ctx, claim):
        attempts.append(claim.attempt)
        raise RuntimeError("transient failure")

    def bad(_ctx, _claim):
        raise jobs.PermanentJobError("invalid input")

    flaky_job = jobs.enqueue(ctx, "flaky", {}, "flaky-0001")
    states = [jobs.run_once(ctx, "w", {"flaky": flaky})["state"] for _ in range(3)]
    assert states == ["queued", "queued", "dead"] and attempts == [1, 2, 3]
    assert "transient failure" in _job(ctx, flaky_job.id).last_error
    assert jobs.run_once(ctx, "w", {"flaky": flaky}) is None
    bad_job = jobs.enqueue(ctx, "bad", {}, "bad-00001")
    assert jobs.run_once(ctx, "w", {"bad": bad})["state"] == "dead"
    assert _job(ctx, bad_job.id).attempts == 1

    # a worker that vanishes without reporting: leases expire, attempts stay bounded
    vanished = jobs.enqueue(ctx, "noop", {}, "vanish-0001")
    for _ in range(ctx.settings.job_max_attempts):
        assert jobs.claim(ctx, "crashy") is not None
        clock.advance(ctx.settings.job_lease_s + 1)
    assert jobs.claim(ctx, "crashy") is None
    j = _job(ctx, vanished.id)
    assert j.state == "dead" and "abandoned" in j.last_error


# ------------------------------------------------------------------------- analysis publication
@pytest.mark.acceptance("AC-S7-09")
def test_crash_inside_publication_leaves_nothing_and_the_retry_publishes_once(ctx, monkeypatch):
    c, r = _submit(ctx)
    analysis_jobs.request_analysis(ctx, r.measurement_id, AnalysisRequest(), c.id)

    def die_before_commit(session, *a, **k):
        session.flush()  # run and artifact rows are written inside the transaction ...
        raise RuntimeError("worker killed before COMMIT")  # ... and the worker dies

    real = jobs.complete_in
    monkeypatch.setattr(jobs, "complete_in", die_before_commit)
    out = jobs.run_once(ctx, "worker-1", analysis_jobs.HANDLERS)
    assert out["state"] == "queued"
    assert _counts(ctx) == (0, 0, 0)  # nothing partial is visible
    monkeypatch.setattr(jobs, "complete_in", real)

    out = jobs.run_once(ctx, "worker-2", analysis_jobs.HANDLERS)
    assert out["state"] == "succeeded" and out["result"]["science_cached"]  # immutable science reused
    assert _counts(ctx) == (2, 5, 1)  # run A + run B; 2 + 2 analysis JSON + 1 comparison
    assert jobs.run_once(ctx, "worker-3", analysis_jobs.HANDLERS) is None

    # the same request again: same job, nothing new
    again = analysis_jobs.request_analysis(ctx, r.measurement_id, AnalysisRequest(), c.id)
    assert again["state"] == "succeeded" and _counts(ctx)[:2] == (2, 5)

    with ctx.sessions() as session:
        manifest = build_manifest(session, r.measurement_id)
    assert validation_errors(manifest, "manifest") == []
    assert len(manifest["categories"]["analysis_runs"]) == 4
    assert len(manifest["categories"]["quality_evaluation"]) == 1
    for a in artifacts(ctx, r.measurement_id, run=True):
        doc = _doc(ctx, a)
        if a.kind == "analysis_json":
            assert validation_errors(doc, "analysis") == [] and doc["run_id"] == a.run_id
            assert doc["record"]["record_code"] == r.record_code


@pytest.mark.acceptance("AC-S7-09")
def test_stale_worker_cannot_publish_after_its_lease_was_taken_over(ctx, clock, monkeypatch):
    c, r = _submit(ctx)
    q = analysis_jobs.request_analysis(ctx, r.measurement_id, AnalysisRequest(), c.id)
    real = analysis_jobs.run_analysis
    takeover: dict = {}

    def stalled(*a, **k):
        res = real(*a, **k)
        if not takeover:
            takeover["started"] = True
            clock.advance(ctx.settings.job_lease_s + 1)  # worker A stalls past its lease ...
            takeover["b"] = jobs.run_once(ctx, "worker-b", analysis_jobs.HANDLERS)  # ... B recovers it
        return res

    monkeypatch.setattr(analysis_jobs, "run_analysis", stalled)
    a = jobs.run_once(ctx, "worker-a", analysis_jobs.HANDLERS)
    assert takeover["b"]["state"] == "succeeded"
    assert a["state"] == "lease_lost"  # A's publication transaction was fenced and rolled back
    assert _counts(ctx) == (2, 5, 1)
    j = _job(ctx, q["job_id"])
    assert (
        j.state == "succeeded" and j.attempts == 2 and j.result["run_id"] == takeover["b"]["result"]["run_id"]
    )


@pytest.mark.acceptance("AC-S7-12")
def test_analysis_references_its_metadata_snapshot(ctx, clock):
    c, r = _submit(ctx)
    req = AnalysisRequest(robustness_check=False)
    q1 = analysis_jobs.request_analysis(ctx, r.measurement_id, req, c.id)  # pinned to revision 1
    with ctx.sessions() as session, session.begin():
        m = session.get(Measurement, r.measurement_id)
        correct_metadata(
            session,
            m,
            MeasurementMetadata(latitude=19.5, longitude=-99.2),
            "re-surveyed",
            "Example Observatory",
            clock(),
        )
    q2 = analysis_jobs.request_analysis(ctx, r.measurement_id, req, c.id)
    assert q1["job_id"] != q2["job_id"]
    assert jobs.run_once(ctx, "w", analysis_jobs.HANDLERS)["state"] == "succeeded"
    assert jobs.run_once(ctx, "w", analysis_jobs.HANDLERS)["state"] == "succeeded"
    run1, run2 = (_job(ctx, q["job_id"]).result["run_id"] for q in (q1, q2))
    assert run1 != run2
    with ctx.sessions() as session:
        revs = {x.run_id: x.metadata_revision for x in session.scalars(select(AnalysisRun))}
    assert revs == {run1: 1, run2: 2}
    docs = {
        a.run_id: _doc(ctx, a) for a in artifacts(ctx, r.measurement_id, run=True) if a.method == "standard"
    }
    assert docs[run1]["record"]["metadata_revision"] == 1 and docs[run1]["record"]["latitude"] == 19.4326
    assert docs[run2]["record"]["metadata_revision"] == 2 and docs[run2]["record"]["latitude"] == 19.5
    with ctx.sessions() as session:
        assert session.get(Measurement, r.measurement_id).record_code == r.record_code


# --------------------------------------------------------------------------------- compute budget
@pytest.mark.acceptance("AC-S7-15")
def test_compute_ceilings_reject_expensive_requests_with_advice(make_ctx):
    ctx = make_ctx("budget", max_candidate_windows=50)
    c, r = _submit(ctx)
    with pytest.raises(analysis_jobs.ComputeBudgetExceeded) as exc:
        analysis_jobs.request_analysis(ctx, r.measurement_id, AnalysisRequest(), c.id)
    msg = str(exc.value)
    assert "111 candidate windows exceed the limit of 50" in msg  # (600 s - 50 s) / 5 s + 1
    assert "window step" in msg and "offline" in msg and "never silently shortened" in msg
    assert _job_count(ctx) == 0
    base = AnalysisConfig()
    wider = base.model_copy(update={"windowing": base.windowing.model_copy(update={"step_s": 12.0})})
    ok = analysis_jobs.request_analysis(ctx, r.measurement_id, AnalysisRequest(config=wider), c.id)
    assert ok["estimate"]["max_candidate_windows"] == 46 and ok["state"] == "queued"

    for name, kw, req, phrase in (
        ("boot", {"max_bootstrap_replicates": 500}, AnalysisRequest(), "bootstrap replicates"),
        ("tmpl", {"max_template_comparisons": 1000}, AnalysisRequest(), "template comparisons"),
        ("runs", {"max_parameter_runs": 1}, AnalysisRequest(), "parameter runs"),
    ):
        ctx2 = make_ctx(name, **kw)
        c2, r2 = _submit(ctx2, f"{name}-00001")
        with pytest.raises(analysis_jobs.ComputeBudgetExceeded, match=phrase):
            analysis_jobs.request_analysis(ctx2, r2.measurement_id, req, c2.id)
    single = analysis_jobs.request_analysis(
        ctx2, r2.measurement_id, AnalysisRequest(robustness_check=False), c2.id
    )
    assert single["estimate"]["parameter_runs"] == 1

    token, _ = new_contributor(ctx)
    resp = TestClient(create_app(ctx)).post(
        f"/api/v1/measurements/{r.measurement_id}/analyses",
        json=AnalysisRequest().model_dump(mode="json"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422 and resp.json()["detail"]["error"] == "compute_budget"
    assert resp.json()["detail"]["estimate"]["max_candidate_windows"] == 111

    # the worker re-checks: a job queued under looser limits is refused, not shortened, under tighter ones
    strict = AppContext(
        ctx.settings.model_copy(update={"max_candidate_windows": 10}), ctx.sessions, ctx.store, ctx.clock
    )
    out = jobs.run_once(strict, "w", analysis_jobs.HANDLERS)
    assert out["state"] == "dead" and "candidate windows exceed" in _job(ctx, ok["job_id"]).last_error
    assert _counts(ctx)[:2] == (0, 0)


def _job_count(ctx) -> int:
    with ctx.sessions() as session:
        return session.scalar(select(func.count()).select_from(Job))
