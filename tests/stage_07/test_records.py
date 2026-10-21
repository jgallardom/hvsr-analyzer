"""Record codes, metadata revisions, consent, licensing manifests, and moderation."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hvsr_core.jsonutil import sha256_bytes
from hvsr_core.metadata import MeasurementMetadata
from hvsr_core.schema_validation import validation_errors
from hvsr_web import moderation
from hvsr_web.api import create_app
from hvsr_web.db import Measurement, MetadataRevision, ModerationEvent, QuotaEntry, RecordCodeCounter
from hvsr_web.licensing import (
    CONSENT_TEXT,
    CONSENT_TEXT_SHA256,
    CONSENT_VERSION,
    REMOVAL_NOTICE,
    ConsentForm,
    ConsentRequired,
)
from hvsr_web.manifests import build_manifest
from hvsr_web.records import allocate_record_code, correct_metadata, metadata_snapshot
from hvsr_web.submissions import submit

from stage7_support import (
    CSV_GZ,
    artifacts,
    committed_objects,
    csv_gz_files,
    fixture_bytes,
    new_admin,
    new_contributor,
    request,
)


@pytest.mark.acceptance("AC-S7-11")
def test_record_code_suffixes_are_allocated_transactionally(ctx):
    base = "9q8yy-2026.8000"
    barrier, codes, lock = threading.Barrier(8), [], threading.Lock()

    def worker():
        barrier.wait()
        with ctx.sessions() as session, session.begin():
            code, _ = allocate_record_code(session, base)
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [base] + [f"{base}-{n:02d}" for n in range(2, 9)]
    with ctx.sessions() as session:
        assert session.get(RecordCodeCounter, base).last_n == 8

    # an aborted transaction does not consume a suffix, and an accepted code is never reused
    _, c = new_contributor(ctx)
    a = submit(ctx, c, request("code-00001"), csv_gz_files())
    b = submit(ctx, c, request("code-00002"), csv_gz_files())
    assert b.record_code == a.record_code + "-02"
    with ctx.sessions() as session:
        session.begin()
        assert allocate_record_code(session, a.record_code) == (a.record_code + "-03", 3)
        session.rollback()
    with ctx.sessions() as session:
        codes = session.scalars(select(Measurement.record_code)).all()
        assert session.get(RecordCodeCounter, a.record_code).last_n == 2
    assert len(codes) == len(set(codes)) == 2


@pytest.mark.acceptance("AC-S7-12")
def test_metadata_corrections_are_versioned_and_keep_the_record_code(ctx, clock):
    _, c = new_contributor(ctx)
    r = submit(ctx, c, request("meta-00001"), csv_gz_files())
    clock.advance(3600)
    corrected = MeasurementMetadata(
        latitude=19.4330, longitude=-99.1330, wind="low", observations="sensor re-levelled"
    )
    with ctx.sessions() as session, session.begin():
        m = session.get(Measurement, r.measurement_id)
        rev = correct_metadata(
            session, m, corrected, "coordinates re-surveyed", "Example Observatory", clock()
        )
    assert rev.revision == 2
    with ctx.sessions() as session:
        m = session.get(Measurement, r.measurement_id)
        assert m.record_code == r.record_code and m.current_revision == 2
        first = metadata_snapshot(session, m.id, 1)
        assert first.metadata_doc["latitude"] == 19.4326 and first.reason == "initial submission"
        assert metadata_snapshot(session, m.id).metadata_doc["observations"] == "sensor re-levelled"
        doc = build_manifest(session, m.id)
    assert doc["metadata_revision"] == 2 and doc["metadata"]["latitude"] == 19.4330
    assert [x["revision"] for x in doc["provenance"]["metadata_revisions"]] == [1, 2]
    history = doc["licensing"]["modification_history"]
    assert [h["action"] for h in history] == ["submitted", "metadata_corrected"]
    assert history[1]["reason"] == "coordinates re-surveyed"
    with ctx.sessions() as session, session.begin(), pytest.raises(ValueError):
        correct_metadata(session, session.get(Measurement, r.measurement_id), corrected, " ", "x", clock())
    # analyses pin their snapshot: see test_jobs.test_analysis_references_its_metadata_snapshot


@pytest.mark.acceptance("AC-S7-13")
def test_consent_is_unchecked_by_default_required_and_recorded(ctx):
    form = ConsentForm()
    assert form.consent_checked is False and form.rights_confirmed is False
    assert len(form.problems()) == 3
    _, c = new_contributor(ctx)
    for kw in ({"consent": False}, {"rights": False}, {"attribution": "   "}):
        with pytest.raises(ConsentRequired):
            submit(ctx, c, request("noconsent1", **kw), csv_gz_files())
    with ctx.sessions() as session:
        assert session.scalar(select(func.count()).select_from(QuotaEntry)) == 0
        assert session.scalar(select(func.count()).select_from(Measurement)) == 0
    assert committed_objects(ctx) == {} and not ctx.store.list("staging/")  # refused before any work

    stale = request("stale-0001")
    stale = stale.model_copy(
        update={"consent": stale.consent.model_copy(update={"consent_version": "consent-v0"})}
    )
    with pytest.raises(ConsentRequired, match="consent version"):
        submit(ctx, c, stale, csv_gz_files())

    client = TestClient(create_app(ctx))
    limits = client.get("/api/v1/limits").json()
    assert limits["consent"] == {"version": CONSENT_VERSION, "text": CONSENT_TEXT}
    r = submit(ctx, c, request("consent-01"), csv_gz_files())
    with ctx.sessions() as session:
        lic = session.get(Measurement, r.measurement_id).licensing
    assert lic["consent_text"] == CONSENT_TEXT and lic["consent_version"] == CONSENT_VERSION
    assert lic["consent_text_sha256"] == sha256_bytes(CONSENT_TEXT.encode()) == CONSENT_TEXT_SHA256
    assert lic["rights_confirmed"] is True


@pytest.mark.acceptance("AC-S7-08")
def test_manifest_carries_license_consent_attribution_and_history(ctx):
    _, c = new_contributor(ctx)
    req = request("lic-000001", attribution="Instituto de Geofisica")
    req = req.model_copy(
        update={"consent": req.consent.model_copy(update={"source_citation": "Field campaign 2026"})}
    )
    r = submit(ctx, c, req, csv_gz_files())
    with ctx.sessions() as session:
        doc = build_manifest(session, r.measurement_id, "moderation@example.org")
    assert validation_errors(doc, "manifest") == []
    lic = doc["licensing"]
    assert (
        lic["license_id"] == "CC-BY-4.0"
        and lic["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    )
    assert lic["attribution"] == "Instituto de Geofisica" and lic["source_citation"] == "Field campaign 2026"
    assert lic["consent_version"] == CONSENT_VERSION and lic["consent_text"] == CONSENT_TEXT
    assert lic["consent_utc"].endswith("Z") and lic["rights_confirmed"] is True
    assert lic["modification_history"][0]["action"] == "submitted"
    assert lic["removal_notice"] == REMOVAL_NOTICE and "third-party" in lic["third_party_note"].lower()
    assert doc["moderation"] == {"status": "public", "contact": "moderation@example.org"}
    record = next(e for e in doc["categories"]["licensing"] if e["kind"] == "license_record")
    stored = next(a for a in artifacts(ctx, r.measurement_id) if a.id == record["artifact_id"])
    assert b"CC-BY-4.0" in ctx.store.get(stored.blob_key)  # an independent licensing artifact as well
    assert all(doc["categories"][k] == [] for k in ("spectra", "plots", "templates"))


@pytest.mark.acceptance("AC-S7-16")
def test_moderation_report_remove_restore_and_purge(ctx):
    _, c = new_contributor(ctx)
    keep = submit(ctx, c, request("mod-keep01", lat=40.0, lon=-3.7), csv_gz_files())
    r = submit(ctx, c, request("mod-000001"), csv_gz_files())
    admin_token, admin = new_admin(ctx)
    client = TestClient(create_app(ctx))

    rep = client.post(
        f"/api/v1/measurements/{r.measurement_id}/reports", data={"reason": "contains a private name"}
    )
    assert rep.status_code == 202
    assert (
        client.get(f"/api/v1/measurements/{r.measurement_id}/manifest").json()["moderation"]["status"]
        == "under_review"
    )
    assert (
        client.post(f"/api/v1/admin/measurements/{r.measurement_id}/remove", data={"reason": "x"}).status_code
        == 401
    )
    assert (
        client.post(
            f"/api/v1/admin/measurements/{r.measurement_id}/remove",
            data={"reason": "x"},
            headers={"Authorization": f"Bearer {c.id}"},
        ).status_code
        == 401
    )
    rm = client.post(
        f"/api/v1/admin/measurements/{r.measurement_id}/remove",
        data={"reason": "private information confirmed"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert rm.status_code == 200
    doc = client.get(f"/api/v1/measurements/{r.measurement_id}/manifest").json()
    assert doc["moderation"]["status"] == "removed" and doc["metadata"] == {}
    entries = [e for cat in doc["categories"].values() for e in cat]
    assert entries and all(e["availability"] == "removed" for e in entries)
    original_url = doc["categories"]["original_data"][0]["url"]
    gone = client.get(original_url)
    assert gone.status_code == 410 and gone.json()["notice"] == REMOVAL_NOTICE
    assert "cannot recall copies" in REMOVAL_NOTICE and "does not revoke" in REMOVAL_NOTICE
    assert doc["licensing"]["modification_history"][-1]["action"] == "removed"

    with ctx.sessions() as session, session.begin():
        moderation.restore(session, admin, r.measurement_id, "false positive", ctx.clock())
    assert client.get(original_url, follow_redirects=False).status_code == 307
    with ctx.sessions() as session, session.begin():
        moderation.remove(session, admin, r.measurement_id, "confirmed again", ctx.clock())

    with pytest.raises(moderation.ModerationError, match="type the record code"):
        moderation.purge(ctx, admin, r.measurement_id, "wrong", "purge requested")
    with pytest.raises(moderation.ModerationError, match="only a removed"):
        moderation.purge(ctx, admin, keep.measurement_id, keep.record_code, "purge requested")
    shared_before = {a.blob_key for a in artifacts(ctx, keep.measurement_id) if a.blob_key}
    out = moderation.purge(ctx, admin, r.measurement_id, r.record_code, "permanent deletion requested")
    assert out["dangling_references"] == 0
    # objects shared with another measurement (same file bytes) are kept; the rest are deleted
    assert set(out["kept_shared_objects"]) <= shared_before
    assert all(ctx.store.exists(k) for k in shared_before)
    assert ctx.store.get(
        next(a for a in artifacts(ctx, keep.measurement_id) if a.kind == "original_csv_gz").blob_key
    ) == fixture_bytes(CSV_GZ)
    with ctx.sessions() as session:
        actions = session.scalars(
            select(ModerationEvent.action)
            .where(ModerationEvent.measurement_id == r.measurement_id)
            .order_by(ModerationEvent.created_utc)
        ).all()
        assert session.get(Measurement, r.measurement_id).record_code == r.record_code  # never reused
        assert session.get(MetadataRevision, (r.measurement_id, 1)) is not None
    assert actions.count("report") == 1 and actions.count("remove") == 2 and actions[-1] == "purge"
    with (
        ctx.sessions() as session,
        session.begin(),
        pytest.raises(moderation.ModerationError, match="purged"),
    ):
        moderation.restore(session, admin, r.measurement_id, "undo", ctx.clock())
