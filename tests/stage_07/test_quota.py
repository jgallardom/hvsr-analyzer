"""Quota ledger, idempotency, guest pool, byte metering, and configurable input ceilings."""

from __future__ import annotations

import gzip
import threading

import pytest
from sqlalchemy import select

from hvsr_web import moderation, quota, submissions
from hvsr_web.auth import GUEST_POLICY, AuthError, guest_pool
from hvsr_web.db import Base, QuotaEntry
from hvsr_web.settings import Settings
from hvsr_web.submissions import LimitExceeded, UploadFile, submit

from stage7_support import (
    CSV_GZ,
    CSV_PLAIN,
    MSEED,
    committed_objects,
    csv_gz_files,
    csv_gz_logical_bytes,
    fixture_bytes,
    new_admin,
    new_contributor,
    request,
)


def _four_channel_mseed() -> bytes:
    import io

    import numpy as np
    from obspy import Stream, Trace, UTCDateTime

    st = Stream()
    for cha in ("HHN", "HHE", "HHZ", "HH1"):
        tr = Trace(data=np.arange(1000, dtype=np.int32))
        tr.stats.network, tr.stats.station, tr.stats.channel = "XX", "TEST", cha
        tr.stats.sampling_rate, tr.stats.starttime = 50.0, UTCDateTime(2026, 1, 1)
        st.append(tr)
    buf = io.BytesIO()
    st.write(buf, format="MSEED", encoding="STEIM2")
    return buf.getvalue()


def _usage(ctx, c):
    with ctx.sessions() as session:
        return quota.usage(session, c, ctx.settings, ctx.clock())


def _entries(ctx, c):
    with ctx.sessions() as session:
        return list(session.scalars(select(QuotaEntry).where(QuotaEntry.contributor_id == c.id)))


@pytest.mark.acceptance("AC-S7-01")
def test_concurrent_reservations_never_exceed_the_monthly_budget(ctx):
    _, c = new_contributor(ctx, quota_bytes=10_000)
    barrier, results, lock = threading.Barrier(16), [], threading.Lock()

    def worker(i):
        barrier.wait()
        try:
            with ctx.sessions() as session, session.begin():
                quota.reserve(session, c, ctx.settings, f"key-{i:02d}", 1000, ctx.clock())
            outcome = "ok"
        except quota.QuotaExceeded:
            outcome = "refused"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 10 and results.count("refused") == 6
    u = _usage(ctx, c)
    assert u["reserved_bytes"] + u["committed_bytes"] == 10_000 <= u["limit_bytes"]
    assert u["remaining_bytes"] == 0


@pytest.mark.acceptance("AC-S7-01", "AC-S7-11")
def test_concurrent_full_submissions_respect_quota_and_get_distinct_codes(ctx):
    logical = csv_gz_logical_bytes()
    _, c = new_contributor(ctx, quota_bytes=int(2.5 * logical))
    barrier, results, lock = threading.Barrier(3), [], threading.Lock()

    def worker(i):
        barrier.wait()
        try:
            r = submit(ctx, c, request(f"parallel-{i:04d}"), csv_gz_files())
            outcome = ("ok", r.record_code)
        except quota.QuotaExceeded:
            outcome = ("refused", None)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok = [code for status, code in results if status == "ok"]
    assert len(ok) == 2 and len(set(ok)) == 2
    assert sorted(ok)[1] == sorted(ok)[0] + "-02"  # same site and upload time: transactional suffix
    u = _usage(ctx, c)
    assert u["committed_bytes"] == 2 * logical and u["reserved_bytes"] == 0


@pytest.mark.acceptance("AC-S7-02")
def test_retry_with_same_key_is_not_charged_twice_and_removal_does_not_refund(ctx):
    _, c = new_contributor(ctx)
    r1 = submit(ctx, c, request("retry-0001"), csv_gz_files())
    r2 = submit(ctx, c, request("retry-0001"), csv_gz_files())
    assert r1.newly_charged and r1.charged_logical_bytes == csv_gz_logical_bytes()
    assert r2.existing and not r2.newly_charged and r2.charged_logical_bytes == 0
    assert r2.measurement_id == r1.measurement_id and r2.record_code == r1.record_code
    assert len(_entries(ctx, c)) == 1
    charged = _usage(ctx, c)["committed_bytes"]
    assert charged == r1.charged_logical_bytes

    _, admin = new_admin(ctx)
    with ctx.sessions() as session, session.begin():
        moderation.remove(session, admin, r1.measurement_id, "contributor asked for removal", ctx.clock())
    assert _usage(ctx, c)["committed_bytes"] == charged
    moderation.purge(ctx, admin, r1.measurement_id, r1.record_code, "permanent deletion requested")
    assert _usage(ctx, c)["committed_bytes"] == charged  # deletion never refunds allowance


@pytest.mark.acceptance("AC-S7-02", "AC-S7-04")
def test_failed_submission_releases_its_reservation_and_retry_charges_once(ctx, monkeypatch):
    _, c = new_contributor(ctx)
    real = submissions._put_verified
    monkeypatch.setattr(
        submissions, "_put_verified", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    with pytest.raises(OSError, match="disk full"):
        submit(ctx, c, request("flaky-0001"), csv_gz_files())
    u = _usage(ctx, c)
    assert u["reserved_bytes"] == 0 and u["committed_bytes"] == 0
    assert [e.state for e in _entries(ctx, c)] == ["released"]
    assert not [i for i in ctx.store.list("") if i.key.startswith("staging/")]

    monkeypatch.setattr(submissions, "_put_verified", real)
    r = submit(ctx, c, request("flaky-0001"), csv_gz_files())
    assert r.newly_charged
    assert [e.state for e in _entries(ctx, c)] == ["committed"]
    assert _usage(ctx, c)["committed_bytes"] == csv_gz_logical_bytes()


@pytest.mark.acceptance("AC-S7-03")
def test_guest_pool_is_best_effort_and_makes_no_per_person_claim(make_ctx):
    disabled = make_ctx("noguest")
    with disabled.sessions() as session, session.begin(), pytest.raises(AuthError):
        guest_pool(session, disabled.settings.guest_uploads_enabled)

    ctx = make_ctx("guest", guest_uploads_enabled=True, guest_pool_monthly_bytes=5 * 1024 * 1024)
    with ctx.sessions() as session, session.begin():
        g1 = guest_pool(session, True)
    with ctx.sessions() as session, session.begin():
        g2 = guest_pool(session, True)
    assert g1.id == g2.id and g1.kind == "guest_pool" and g1.token_sha256 is None
    r = submit(ctx, g1, request("guest-0001"), csv_gz_files())
    u = _usage(ctx, g1)
    assert u["guest_pool"] and u["limit_bytes"] == 5 * 1024 * 1024
    assert u["committed_bytes"] == r.charged_logical_bytes
    assert "not metered per person" in GUEST_POLICY and "does not identify a person" in GUEST_POLICY
    # the schema has nowhere to store an IP address or cookie identifier
    columns = {c.name for t in Base.metadata.sorted_tables for c in t.columns}
    assert not [c for c in columns if "ip" in c.split("_") or "cookie" in c or "fingerprint" in c]


@pytest.mark.acceptance("AC-S7-14")
def test_quota_charges_logical_bytes_and_meters_stored_and_transfer_separately(ctx):
    _, c = new_contributor(ctx)
    gz = submit(ctx, c, request("gz-000001"), csv_gz_files())
    plain_bytes = fixture_bytes(CSV_PLAIN)
    plain = submit(
        ctx,
        c,
        request("plain-0001", csv_layout="utc_timestamps", csv_timestamps_declared_utc=True),
        [UploadFile(CSV_PLAIN, plain_bytes)],
    )
    ms_bytes = fixture_bytes(MSEED)
    ms = submit(ctx, c, request("mseed-0001", lat=-33.45, lon=-70.66), [UploadFile(MSEED, ms_bytes)])

    # compressed input is charged at its uncompressed size: compression is not a quota bypass
    assert gz.charged_logical_bytes == len(gzip.decompress(fixture_bytes(CSV_GZ)))
    assert gz.transfer_bytes == len(fixture_bytes(CSV_GZ)) < gz.charged_logical_bytes
    assert plain.charged_logical_bytes == len(plain_bytes) == plain.transfer_bytes
    assert plain.stored_bytes < len(plain_bytes)  # deterministic gzip of the original is what is stored
    # Steim2 MiniSEED: charged at decoded int32 sample bytes when that exceeds the file size
    assert ms.charged_logical_bytes == 3 * 30_000 * 4 > len(ms_bytes) == ms.transfer_bytes

    by_key = {e.idempotency_key: e for e in _entries(ctx, c)}
    for key, r in (("gz-000001", gz), ("plain-0001", plain), ("mseed-0001", ms)):
        e = by_key[key]
        assert (e.logical_bytes, e.stored_bytes, e.transfer_bytes) == (
            r.charged_logical_bytes,
            r.stored_bytes,
            r.transfer_bytes,
        )
    assert _usage(ctx, c)["committed_bytes"] == sum(r.charged_logical_bytes for r in (gz, plain, ms))


@pytest.mark.acceptance("AC-S7-15")
def test_input_ceilings_come_from_environment_and_are_enforced(make_ctx):
    env = {
        "HVSR_MAX_REQUEST_BYTES": "1000",
        "HVSR_MAX_FILES": "1",
        "HVSR_MAX_DURATION_S": "300",
        "HVSR_MAX_SAMPLES_PER_CHANNEL": "5000",
        "HVSR_MAX_DECODED_BYTES": "4096",
        "HVSR_MAX_DECOMPRESSION_RATIO": "3",
        "HVSR_MAX_CHANNELS": "3",
        "HVSR_MAX_CONCURRENT_JOBS": "2",
        "HVSR_MAX_CANDIDATE_WINDOWS": "50",
        "HVSR_MAX_BOOTSTRAP_REPLICATES": "500",
        "HVSR_MAX_TEMPLATE_COMPARISONS": "1000",
        "HVSR_JOB_WALL_TIME_S": "60",
        "HVSR_JOB_CPU_TIME_S": "30",
    }
    s = Settings.from_env(env)
    assert s.max_request_bytes == 1000 and s.max_files == 1 and s.max_concurrent_jobs == 2
    assert s.max_candidate_windows == 50 and s.max_template_comparisons == 1000
    assert s.job_wall_time_s == 60 and s.job_cpu_time_s == 30
    lim = s.input_limits()
    assert (lim.max_decoded_bytes, lim.max_decompression_ratio, lim.max_channels) == (4096, 3.0, 3)
    assert "database_url" not in s.public_limits() and "url_signing_key" not in s.public_limits()

    def attempt(name, **overrides):
        ctx = make_ctx(name, **overrides)
        _, c = new_contributor(ctx)
        with pytest.raises(LimitExceeded) as exc:
            submit(ctx, c, request(f"{name}-0001"), csv_gz_files())
        return exc.value.code

    assert attempt("req", max_request_bytes=1000) == "request_size_limit"
    assert attempt("dur", max_duration_s=300) == "duration_limit"
    assert attempt("smp", max_samples_per_channel=5000) == "sample_limit"
    assert attempt("dec", max_decoded_bytes=4096) == "decoded_size_limit"
    ctx = make_ctx("chan", max_channels=3)
    _, c = new_contributor(ctx)
    with pytest.raises(LimitExceeded) as exc:
        submit(ctx, c, request("chan-00001"), [UploadFile("four.mseed", _four_channel_mseed())])
    assert exc.value.code == "channel_limit"
    ctx = make_ctx("files", max_files=1)
    _, c = new_contributor(ctx)
    with pytest.raises(LimitExceeded, match="files exceed"):
        submit(
            ctx, c, request("files-0001"), [*csv_gz_files(), UploadFile("extra.xml", b"<x/>", "stationxml")]
        )
    # a decompression bomb (9 MiB of identical rows, ~1000:1) hits the default 200:1 ratio ceiling
    bomb = gzip.compress(b"time_s,N,E,Z\n" + b"0,0,0,0\n" * (9 * 1024 * 1024 // 8), mtime=0)
    ctx = make_ctx("ratio")
    _, c = new_contributor(ctx)
    with pytest.raises(LimitExceeded) as exc:
        submit(ctx, c, request("ratio-0001"), [UploadFile("bomb.csv.gz", bomb)])
    assert exc.value.code == "decompression_ratio_limit"
    assert _usage(ctx, c)["committed_bytes"] == 0 and not _entries(ctx, c)  # refused before any charge
    assert not committed_objects(ctx)
