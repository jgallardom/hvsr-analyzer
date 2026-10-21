"""One full backup/restore cycle into a fresh database and object store."""

from __future__ import annotations

import json

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import select

from hvsr_core.config import AnalysisRequest
from hvsr_core.jsonutil import sha256_bytes
from hvsr_core.metadata import MeasurementMetadata
from hvsr_web import analysis_jobs, jobs, moderation
from hvsr_web.api import create_app
from hvsr_web.backup import BackupError, create_backup, restore_backup
from hvsr_web.db import Measurement, alembic_config
from hvsr_web.manifests import build_manifest
from hvsr_web.records import correct_metadata
from hvsr_web.submissions import UploadFile, submit

from stage7_support import (
    CSV_GZ,
    MSEED,
    committed_objects,
    csv_gz_files,
    fixture_bytes,
    new_admin,
    new_contributor,
    request,
)


def _manifests(ctx) -> dict:
    with ctx.sessions() as session:
        ids = session.scalars(select(Measurement.id).order_by(Measurement.id)).all()
        return {i: build_manifest(session, i) for i in ids}


@pytest.mark.acceptance("AC-S7-10")
def test_backup_and_restore_cycle(make_ctx, tmp_path, clock):
    src = make_ctx("source")
    token, c = new_contributor(src)
    a = submit(src, c, request("backup-001"), csv_gz_files())
    b = submit(src, c, request("backup-002", lat=-33.4, lon=-70.6), [UploadFile(MSEED, fixture_bytes(MSEED))])
    analysis_jobs.request_analysis(src, a.measurement_id, AnalysisRequest(), c.id)
    assert jobs.run_once(src, "w", analysis_jobs.HANDLERS)["state"] == "succeeded"
    with src.sessions() as session, session.begin():
        correct_metadata(
            session,
            session.get(Measurement, a.measurement_id),
            MeasurementMetadata(latitude=19.44, longitude=-99.13),
            "GPS re-read",
            "Example Observatory",
            clock(),
        )
    _, admin = new_admin(src)
    with src.sessions() as session, session.begin():
        moderation.remove(session, admin, b.measurement_id, "duplicate upload", clock())

    manifest = create_backup(src, tmp_path / "backup")
    assert (
        manifest["tables"]["measurements"]["rows"] == 2 and manifest["tables"]["analysis_runs"]["rows"] == 2
    )
    assert manifest["objects"] and not [k for k in manifest["objects"] if k.startswith("staging/")]

    dst = make_ctx("restored")  # fresh database (migrated) and fresh object store
    report = restore_backup(dst, tmp_path / "backup")
    assert report["verified"] and report["tables"]["measurements"] == 2
    assert _manifests(dst) == _manifests(src)
    assert committed_objects(dst) == committed_objects(src)

    # the restored system works: downloads, authentication, quota, and record codes continue
    client = TestClient(create_app(dst))
    orig = _manifests(dst)[a.measurement_id]["categories"]["original_data"][0]
    assert sha256_bytes(client.get(orig["url"]).content) == sha256_bytes(fixture_bytes(CSV_GZ))
    assert (
        client.get(_manifests(dst)[b.measurement_id]["categories"]["original_data"][0]["url"]).status_code
        == 410
    )
    with dst.sessions() as session:
        restored_c = session.get(type(c), c.id)
    again = submit(dst, restored_c, request("backup-001"), csv_gz_files())
    assert again.existing and again.measurement_id == a.measurement_id  # idempotency keys survive
    third = submit(dst, restored_c, request("backup-003"), csv_gz_files())
    assert third.record_code == a.record_code + "-02"  # the counter survives; no code reuse

    # restore refuses a non-empty database and a tampered backup
    with pytest.raises(BackupError, match="empty database"):
        restore_backup(dst, tmp_path / "backup")
    victim = next(k for k in manifest["objects"] if k.startswith("recordings/"))
    (tmp_path / "backup" / "objects" / victim).write_bytes(b"tampered")
    with pytest.raises(BackupError, match="checksum mismatch"):
        restore_backup(make_ctx("third"), tmp_path / "backup")
    meta = json.loads((tmp_path / "backup" / "backup.json").read_text())
    head = ScriptDirectory.from_config(alembic_config(str(src.settings.database_url))).get_current_head()
    assert meta["format"] == "hvsr-analyzer-backup/1" and meta["schema_revision"] == head
