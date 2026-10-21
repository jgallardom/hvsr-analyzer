"""Backup and restore: JSON Lines per table plus every committed object, with a SHA-256 manifest.

`create_backup` reads all tables inside one transaction (a consistent snapshot) and then copies
committed objects; committed objects are immutable and written before the rows that reference
them, so every referenced object exists. Staging objects are not backed up.

`restore_backup` verifies every file checksum first, refuses a non-empty database or a schema
revision that differs from the backup, writes objects (identical content may already exist;
different content under the same key aborts), inserts all rows in one transaction, and verifies
the result. It never deletes or overwrites existing data. Copy backups off-host; a backup on the
same disk is not a backup.
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from sqlalchemy import func, select

from hvsr_core.jsonutil import canonical_dumps, sha256_bytes
from hvsr_core.versions import PACKAGE_VERSION

from .context import AppContext
from .db import (
    Admin,
    AnalysisRun,
    Artifact,
    BankEvent,
    ComputeUsage,
    Contributor,
    Job,
    Measurement,
    MetadataRevision,
    ModerationEvent,
    QuotaEntry,
    QuotaPeriod,
    RecordCodeCounter,
    StagedUpload,
    TemplateBankRecord,
    utc_iso,
)
from .storage import STAGING_PREFIX, check_key

FORMAT = "hvsr-analyzer-backup/1"
TABLES = [
    Contributor,
    Admin,
    QuotaPeriod,
    QuotaEntry,
    ComputeUsage,
    RecordCodeCounter,
    Measurement,
    MetadataRevision,
    Artifact,
    AnalysisRun,
    Job,
    StagedUpload,
    ModerationEvent,
    TemplateBankRecord,
    BankEvent,
]


class BackupError(RuntimeError):
    pass


def _rows(session, model) -> list[dict]:
    """Rows in primary-key order, sorted here (code-point order, as SQLite) so a checksum taken on
    one database verifies on another whatever its text collation."""
    table = model.__table__
    pk = [c.name for c in table.primary_key.columns]
    rows = [dict(r) for r in session.execute(select(table)).mappings()]
    return sorted(rows, key=lambda r: tuple(r[c] for c in pk))


def _schema_revision(session) -> str | None:
    return MigrationContext.configure(session.connection()).get_current_revision()


def _lines(rows: list[dict]) -> bytes:
    return "".join(canonical_dumps(r) + "\n" for r in rows).encode()


def create_backup(ctx: AppContext, dest: Path) -> dict:
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=False)
    (dest / "db").mkdir()
    tables = {}
    with ctx.sessions() as session, session.begin():
        revision = _schema_revision(session)
        for model in TABLES:
            data = _lines(_rows(session, model))
            name = model.__tablename__
            (dest / "db" / f"{name}.jsonl").write_bytes(data)
            tables[name] = {"rows": data.count(b"\n"), "sha256": sha256_bytes(data)}
    objects = {}
    for info in ctx.store.list(""):
        if info.key.startswith(STAGING_PREFIX):
            continue
        data = ctx.store.get(info.key)
        path = dest / "objects" / info.key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        objects[info.key] = {"sha256": sha256_bytes(data), "size": len(data)}
    manifest = {
        "format": FORMAT,
        "created_utc": utc_iso(ctx.clock()),
        "software_version": PACKAGE_VERSION,
        "schema_revision": revision,
        "tables": tables,
        "objects": objects,
    }
    (dest / "backup.json").write_text(canonical_dumps(manifest))
    return manifest


def verify_backup(src: Path) -> dict:
    src = Path(src)
    manifest = json.loads((src / "backup.json").read_text())
    if manifest.get("format") != FORMAT:
        raise BackupError(f"unsupported backup format {manifest.get('format')!r}")
    problems = []
    for name, meta in manifest["tables"].items():
        p = src / "db" / f"{name}.jsonl"
        if not p.is_file() or sha256_bytes(p.read_bytes()) != meta["sha256"]:
            problems.append(f"table {name}: missing or checksum mismatch")
    for key, meta in manifest["objects"].items():
        p = src / "objects" / check_key(key)
        if not p.is_file() or sha256_bytes(p.read_bytes()) != meta["sha256"]:
            problems.append(f"object {key}: missing or checksum mismatch")
    if problems:
        raise BackupError("backup verification failed: " + "; ".join(problems[:20]))
    return manifest


def restore_backup(ctx: AppContext, src: Path) -> dict:
    src = Path(src)
    manifest = verify_backup(src)
    with ctx.sessions() as session:
        revision = _schema_revision(session)
        if revision != manifest["schema_revision"]:
            raise BackupError(
                f"database schema revision {revision!r} differs from the backup's {manifest['schema_revision']!r}"
            )
        occupied = [m.__tablename__ for m in TABLES if session.scalar(select(func.count()).select_from(m))]
    if occupied:
        raise BackupError(f"restore needs an empty database; these tables have rows: {occupied}")
    for key in manifest["objects"]:
        ctx.store.put(key, (src / "objects" / key).read_bytes())
    with ctx.sessions() as session, session.begin():
        for model in TABLES:
            rows = [
                json.loads(line)
                for line in (src / "db" / f"{model.__tablename__}.jsonl").read_text().splitlines()
            ]
            if rows:
                session.execute(model.__table__.insert(), rows)
    return verify_restore(ctx, manifest)


def verify_restore(ctx: AppContext, manifest: dict) -> dict:
    problems = []
    with ctx.sessions() as session:
        for model in TABLES:
            name = model.__tablename__
            if sha256_bytes(_lines(_rows(session, model))) != manifest["tables"][name]["sha256"]:
                problems.append(f"table {name} differs after restore")
    for key, meta in manifest["objects"].items():
        try:
            ok = sha256_bytes(ctx.store.get(key)) == meta["sha256"]
        except KeyError:
            ok = False
        if not ok:
            problems.append(f"object {key} missing or different after restore")
    if problems:
        raise BackupError("restore verification failed: " + "; ".join(problems[:20]))
    return {
        "tables": {k: v["rows"] for k, v in manifest["tables"].items()},
        "objects": len(manifest["objects"]),
        "verified": True,
    }
