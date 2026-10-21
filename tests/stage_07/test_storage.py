"""Object storage durability, staging cleanup, stable links, labels, tokens, and the S3 adapter."""

from __future__ import annotations

import gzip
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hvsr_core.analysis import recording_hash
from hvsr_core.io import CSVOptions, read_csv
from hvsr_core.io.preservation import reconstructed_csv, recording_from_canonical
from hvsr_core.jsonutil import sha256_bytes, strict_loads
from hvsr_web import submissions
from hvsr_web.api import create_app
from hvsr_web.auth import AuthError, contributor_for_token, token_digest
from hvsr_web.context import AppContext
from hvsr_web.db import Base, Contributor, StagedUpload
from hvsr_web.manifests import build_manifest
from hvsr_web.storage import ImmutableObjectError, S3ObjectStore, blob_key
from hvsr_web.submissions import UploadFile, submit, sweep_staging

from stage7_support import (
    CSV_GZ,
    CSV_PLAIN,
    MSEED,
    artifacts,
    committed_objects,
    csv_gz_files,
    fixture_bytes,
    new_contributor,
    request,
)


@pytest.mark.acceptance("AC-S7-04")
def test_staging_sweep_removes_only_staging_objects(ctx, clock, monkeypatch):
    _, c = new_contributor(ctx)
    kept = submit(ctx, c, request("kept-00001"), csv_gz_files())
    before = committed_objects(ctx)
    assert before and not [k for k in before if k.startswith("staging/")]

    # an interrupted submission: staged bytes written, crash before the final commit and cleanup
    monkeypatch.setattr(submissions, "_discard", lambda *a, **k: None)
    real_commit = submissions.quota.commit
    monkeypatch.setattr(
        submissions.quota, "commit", lambda *a, **k: (_ for _ in ()).throw(SystemError("crash"))
    )
    with pytest.raises(SystemError):
        submit(ctx, c, request("crash-0001", lat=10.0, lon=10.0), csv_gz_files())
    monkeypatch.setattr(submissions.quota, "commit", real_commit)
    staged = [i.key for i in ctx.store.list("staging/")]
    assert staged  # left behind by the crash
    ctx.store.put("staging/orphan/00", b"no database row")

    young = sweep_staging(ctx)
    assert young["removed"] == [] and young["released_reservations"] == []  # inside the TTL: untouched
    clock.advance(ctx.settings.staging_ttl_s + 60)
    os.utime(ctx.store._path("staging/orphan/00"), (clock.epoch() - ctx.settings.staging_ttl_s - 10,) * 2)
    out = sweep_staging(ctx)
    assert set(out["removed"]) == {*staged, "staging/orphan/00"}
    assert len(out["released_reservations"]) == 1  # the crashed submission's stale reservation
    assert not ctx.store.list("staging/")
    after = committed_objects(ctx)
    assert {k: v for k, v in after.items() if k in before} == before  # committed bytes untouched
    with ctx.sessions() as session:
        states = {su.state for su in session.scalars(select(StagedUpload))}
    assert states <= {"cleaned", "discarded"}
    with ctx.sessions() as session:
        assert build_manifest(session, kept.measurement_id)["record_code"] == kept.record_code


@pytest.mark.acceptance("AC-S7-04")
def test_committed_objects_are_immutable(ctx):
    key = blob_key("metadata", sha256_bytes(b"a"))
    ctx.store.put(key, b"a")
    ctx.store.put(key, b"a")  # identical content is an idempotent no-op
    with pytest.raises(ImmutableObjectError):
        ctx.store.put(key, b"b")
    ctx.store.put("staging/x/00", b"1")
    ctx.store.put("staging/x/00", b"2")  # staging may be rewritten
    assert ctx.store.get("staging/x/00") == b"2"


@pytest.mark.acceptance("AC-S7-05")
def test_committed_archives_and_metadata_survive_restart(make_ctx):
    ctx = make_ctx("persist")
    _, c = new_contributor(ctx)
    r = submit(ctx, c, request("persist-01"), csv_gz_files())
    with ctx.sessions() as session:
        manifest = build_manifest(session, r.measurement_id)
    objects = committed_objects(ctx)
    ctx.sessions.kw["bind"].dispose()
    del ctx

    restarted: AppContext = make_ctx("persist")  # new engine, new store object, migrations re-run
    with restarted.sessions() as session:
        assert build_manifest(session, r.measurement_id) == manifest
    assert committed_objects(restarted) == objects
    for a in artifacts(restarted, r.measurement_id):
        if a.blob_key:
            assert sha256_bytes(restarted.store.get(a.blob_key)) == a.stored_sha256


@pytest.mark.acceptance("AC-S7-06")
def test_manifest_links_are_stable_and_survive_signed_url_expiry(ctx, clock):
    _, c = new_contributor(ctx)
    r = submit(ctx, c, request("links-0001"), csv_gz_files())
    client = TestClient(create_app(ctx))
    manifest = client.get(f"/api/v1/measurements/{r.measurement_id}/manifest").json()
    entries = [e for cat in manifest["categories"].values() for e in cat]
    assert entries and all(e["url"] == f"/api/v1/artifacts/{e['artifact_id']}" for e in entries)
    assert all("sig=" not in e["url"] and "exp=" not in e["url"] for e in entries)
    original = next(e for e in manifest["categories"]["original_data"])

    first = client.get(original["url"], follow_redirects=False)
    assert first.status_code == 307 and first.headers["cache-control"] == "no-store"
    signed = first.headers["location"]
    assert "sig=" in signed and client.get(signed).content == fixture_bytes(CSV_GZ)

    clock.advance(ctx.settings.signed_url_ttl_s + 1)
    assert client.get(signed).status_code == 403  # the short-lived URL expired ...
    again = client.get(original["url"], follow_redirects=False)  # ... the stable link still works
    assert again.status_code == 307 and again.headers["location"] != signed
    body = client.get(again.headers["location"])
    assert body.status_code == 200 and sha256_bytes(body.content) == original["sha256"]
    tampered = signed.replace("sig=", "sig=x")
    assert client.get(tampered).status_code == 403
    assert client.get("/api/v1/artifacts/art_doesnotexist").status_code == 404


@pytest.mark.acceptance("AC-S7-07")
def test_original_canonical_and_reconstructed_labels_survive_storage(ctx):
    _, c = new_contributor(ctx)
    client = TestClient(create_app(ctx))
    by_label = lambda mid: {a.label: a for a in artifacts(ctx, mid)}  # noqa: E731

    gz = submit(ctx, c, request("label-gz01"), csv_gz_files())
    labels = by_label(gz.measurement_id)
    assert labels["Original CSV.GZ"].availability == "available"
    assert labels["Original CSV.GZ"].sha256 == sha256_bytes(fixture_bytes(CSV_GZ))
    assert labels["Canonical recording"].availability == "on_demand"
    assert labels["Normalized waveform CSV"].availability == "on_demand"
    assert "Reconstructed CSV" not in labels

    plain_bytes = fixture_bytes(CSV_PLAIN)
    plain = submit(
        ctx,
        c,
        request("label-csv1", csv_layout="utc_timestamps", csv_timestamps_declared_utc=True),
        [UploadFile(CSV_PLAIN, plain_bytes)],
    )
    orig = by_label(plain.measurement_id)["Original CSV"]
    assert orig.stored_encoding == "gzip" and gzip.decompress(ctx.store.get(orig.blob_key)) == plain_bytes
    resp = client.get(f"/api/v1/artifacts/{orig.id}")
    assert resp.status_code == 200 and resp.content == plain_bytes  # exact original bytes, not the gzip
    assert 'filename="flat_utc_20hz.csv"' in resp.headers["content-disposition"]

    ms = submit(
        ctx, c, request("label-ms01", lat=-33.4, lon=-70.6), [UploadFile(MSEED, fixture_bytes(MSEED))]
    )
    ms_labels = by_label(ms.measurement_id)
    assert ms_labels["Original MiniSEED"].sha256 == sha256_bytes(fixture_bytes(MSEED))
    assert (
        ms_labels["Canonical recording (the original MiniSEED)"].blob_key
        == ms_labels["Original MiniSEED"].blob_key
    )

    co = submit(
        ctx,
        c,
        request("label-co01", lat=1.0, lon=1.0, csv_layout="elapsed_time", canonical_only=True),
        csv_gz_files(),
    )
    co_labels = by_label(co.measurement_id)
    assert co_labels["Original CSV.GZ (not retained)"].availability == "unavailable"
    assert co_labels["Original CSV.GZ (not retained)"].blob_key is None
    assert not [k for k in co_labels if k.startswith("Original") and co_labels[k].availability == "available"]
    canon, side = co_labels["Canonical recording"], co_labels["Canonical recording sidecar"]
    assert canon.availability == "available" and co_labels["Reconstructed CSV"].availability == "on_demand"
    with ctx.sessions() as session:
        doc = build_manifest(session, co.measurement_id)
    assert doc["original_bytes_retained"] is False and "not retained" in doc["original_bytes_note"]
    # the stored canonical recording reproduces the parsed recording exactly
    mseed, sidecar = ctx.store.get(canon.blob_key), strict_loads(ctx.store.get(side.blob_key))
    parsed, _ = read_csv(fixture_bytes(CSV_GZ), CSVOptions(layout="elapsed_time"))
    assert recording_hash(recording_from_canonical(mseed, sidecar)) == recording_hash(parsed)
    text = reconstructed_csv(mseed, sidecar, None).decode()
    assert text.startswith("# Reconstructed CSV generated from the canonical recording")


@pytest.mark.acceptance("AC-S7-17")
def test_tokens_are_hashed_never_in_urls_and_identity_is_private(ctx):
    token, c = new_contributor(ctx, email="private.person@example.org")
    with ctx.sessions() as session:
        row = session.get(Contributor, c.id)
        assert row.token_sha256 == token_digest(token) and token not in repr(vars(row))
        assert contributor_for_token(session, token).id == c.id
        with pytest.raises(AuthError):
            contributor_for_token(session, token + "x")
    columns = {col.name for t in Base.metadata.sorted_tables for col in t.columns}
    assert "token" not in columns and "token_sha256" in columns

    client = TestClient(create_app(ctx))
    files = {"files": (CSV_GZ, fixture_bytes(CSV_GZ), "application/gzip")}
    form = {"submission": request("api-000001").model_dump_json()}
    rejected = client.post(f"/api/v1/submissions?token={token}", data=form, files=files)
    assert rejected.status_code == 400 and rejected.json()["error"] == "credentials_in_url"
    assert client.post("/api/v1/submissions", data=form, files=files).status_code == 401  # guests disabled
    ok = client.post(
        "/api/v1/submissions", data=form, files=files, headers={"Authorization": f"Bearer {token}"}
    )
    assert ok.status_code == 201, ok.text
    mid = ok.json()["measurement_id"]

    manifest = client.get(f"/api/v1/measurements/{mid}/manifest")
    text = manifest.text
    assert "private.person@example.org" not in text and token not in text and row.token_sha256 not in text
    assert manifest.json()["licensing"]["attribution"] == "Example Observatory"
    for e in (e for cat in manifest.json()["categories"].values() for e in cat):
        loc = client.get(e["url"], follow_redirects=False).headers.get("location", "")
        assert token not in loc and "hvu_" not in loc


@pytest.mark.acceptance("AC-S7-18")
def test_s3_adapter_against_emulated_s3_api(make_ctx, monkeypatch):
    import boto3
    import moto
    from botocore.config import Config

    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(k, v)
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
        client.create_bucket(Bucket="hvsr-test")
        store = S3ObjectStore("hvsr-test", client=client)
        key = blob_key("metadata", sha256_bytes(b"payload"))
        store.put(key, b"payload", "application/json")
        store.put(key, b"payload")  # same content: no-op
        with pytest.raises(ImmutableObjectError):
            store.put(key, b"other")
        assert store.get(key) == b"payload" and store.exists(key)
        with pytest.raises(KeyError):
            store.get("metadata/sha256/00/missing")
        url = store.signed_url(key, 60, "x.json", "application/json")
        assert url.startswith("https://") and "X-Amz-Signature" in url and "X-Amz-Expires=60" in url
        store.put("staging/s/00", b"1")
        store.put("staging/s/00", b"2")
        assert store.get("staging/s/00") == b"2"
        assert [i.key for i in store.list("staging/")] == ["staging/s/00"]
        store.delete("staging/s/00")
        assert not store.exists("staging/s/00")

        # the whole submission pipeline on the S3 adapter
        ctx = make_ctx("s3", store=store, object_store="s3", s3_bucket="hvsr-test")
        _, c = new_contributor(ctx)
        r = submit(ctx, c, request("s3-000001"), csv_gz_files())
        with ctx.sessions() as session:
            doc = build_manifest(session, r.measurement_id)
        orig = doc["categories"]["original_data"][0]
        a = next(x for x in artifacts(ctx, r.measurement_id) if x.id == orig["artifact_id"])
        assert store.get(a.blob_key) == fixture_bytes(CSV_GZ)
        assert not store.list("staging/")
        resp = TestClient(create_app(ctx)).get(orig["url"], follow_redirects=False)
        assert resp.status_code == 307 and "X-Amz-Signature" in resp.headers["location"]
