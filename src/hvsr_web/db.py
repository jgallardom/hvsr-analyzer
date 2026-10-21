"""Metadata database: SQLAlchemy models, engine factory, and Alembic migration entry points.

Private contributor identity (token hash, e-mail) lives only in `contributors`; public attribution
is part of each measurement's licensing record. Timestamps used for ordering/leases are epoch
seconds; human-facing timestamps are UTC ISO 8601 strings.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utc_iso(t: dt.datetime | float | None = None) -> str:
    if t is None:
        t = dt.datetime.now(dt.UTC)
    elif isinstance(t, (int, float)):
        t = dt.datetime.fromtimestamp(t, dt.UTC)
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class Contributor(Base):
    __tablename__ = "contributors"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))  # token | guest_pool
    token_sha256: Mapped[str | None] = mapped_column(String(64), unique=True)
    private_email: Mapped[str | None] = mapped_column(String(320))
    private_note: Mapped[str | None] = mapped_column(Text)
    monthly_quota_bytes: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_utc: Mapped[str] = mapped_column(String(20))


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    private_name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_utc: Mapped[str] = mapped_column(String(20))


class QuotaPeriod(Base):
    __tablename__ = "quota_periods"
    contributor_id: Mapped[str] = mapped_column(ForeignKey("contributors.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # UTC YYYY-MM
    limit_bytes: Mapped[int] = mapped_column(Integer)
    reserved_bytes: Mapped[int] = mapped_column(Integer, default=0)
    committed_bytes: Mapped[int] = mapped_column(Integer, default=0)


class QuotaEntry(Base):
    __tablename__ = "quota_entries"
    __table_args__ = (UniqueConstraint("contributor_id", "idempotency_key", name="uq_quota_idem"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contributor_id: Mapped[str] = mapped_column(ForeignKey("contributors.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))  # reserved | committed | released
    logical_bytes: Mapped[int] = mapped_column(Integer)
    stored_bytes: Mapped[int | None] = mapped_column(Integer)
    transfer_bytes: Mapped[int | None] = mapped_column(Integer)
    created_utc: Mapped[str] = mapped_column(String(20))
    updated_utc: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)


class ComputeUsage(Base):
    __tablename__ = "compute_usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contributor_id: Mapped[str | None] = mapped_column(ForeignKey("contributors.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))
    kind: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[str | None] = mapped_column(String(36))
    estimate: Mapped[dict] = mapped_column(JSON)
    cpu_s: Mapped[float | None] = mapped_column(Float)
    wall_s: Mapped[float | None] = mapped_column(Float)
    created_utc: Mapped[str] = mapped_column(String(20))


class RecordCodeCounter(Base):
    __tablename__ = "record_code_counters"
    base_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_n: Mapped[int] = mapped_column(Integer)


class Measurement(Base):
    __tablename__ = "measurements"
    __table_args__ = (UniqueConstraint("contributor_id", "submission_key", name="uq_submission_idem"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    record_code: Mapped[str] = mapped_column(String(40), unique=True)
    base_code: Mapped[str] = mapped_column(String(32), index=True)
    suffix_n: Mapped[int] = mapped_column(Integer)
    geohash: Mapped[str] = mapped_column(String(12))
    decimal_year: Mapped[float] = mapped_column(Float)
    decimal_year_source: Mapped[str] = mapped_column(String(20))
    created_utc: Mapped[str] = mapped_column(String(20))
    contributor_id: Mapped[str] = mapped_column(ForeignKey("contributors.id"), index=True)
    submission_key: Mapped[str] = mapped_column(String(128))
    moderation_status: Mapped[str] = mapped_column(String(16), default="public")
    original_bytes_retained: Mapped[bool] = mapped_column(Boolean)
    original_bytes_note: Mapped[str | None] = mapped_column(Text)
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    site_group_id: Mapped[str | None] = mapped_column(String(64))
    licensing: Mapped[dict] = mapped_column(JSON)
    parse_options: Mapped[dict] = mapped_column(JSON)
    recording_summary: Mapped[dict] = mapped_column(JSON)


class MetadataRevision(Base):
    __tablename__ = "metadata_revisions"
    measurement_id: Mapped[str] = mapped_column(ForeignKey("measurements.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    metadata_doc: Mapped[dict] = mapped_column("metadata", JSON)
    created_utc: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    author_attribution: Mapped[str] = mapped_column(String(200))


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    measurement_id: Mapped[str] = mapped_column(ForeignKey("measurements.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(40), index=True)
    category: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(48))
    label: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str | None] = mapped_column(String(255))
    blob_key: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(100))
    byte_size: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    stored_sha256: Mapped[str | None] = mapped_column(String(64))
    stored_encoding: Mapped[str | None] = mapped_column(String(16))
    availability: Mapped[str] = mapped_column(String(16))
    availability_reason: Mapped[str | None] = mapped_column(Text)
    generated_by: Mapped[dict] = mapped_column(JSON)
    source_refs: Mapped[list] = mapped_column(JSON)
    method: Mapped[str | None] = mapped_column(String(32))
    created_utc: Mapped[str] = mapped_column(String(20))


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    measurement_id: Mapped[str] = mapped_column(ForeignKey("measurements.id"), index=True)
    metadata_revision: Mapped[int] = mapped_column(Integer)
    science_key: Mapped[str] = mapped_column(String(64))
    config_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSON)
    role: Mapped[str] = mapped_column(String(16))  # primary | robustness_b
    primary_run_id: Mapped[str | None] = mapped_column(String(40))
    comparison: Mapped[dict | None] = mapped_column(JSON)
    created_utc: Mapped[str] = mapped_column(String(20))
    job_id: Mapped[str | None] = mapped_column(String(36))


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    contributor_id: Mapped[str | None] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(16), index=True)  # queued | running | succeeded | dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON)


class StagedUpload(Base):
    __tablename__ = "staged_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_key: Mapped[str] = mapped_column(String(128))
    contributor_id: Mapped[str] = mapped_column(ForeignKey("contributors.id"))
    object_key: Mapped[str] = mapped_column(String(255), unique=True)
    state: Mapped[str] = mapped_column(String(16))  # staged | committed | discarded | cleaned
    created_at: Mapped[float] = mapped_column(Float)


class ModerationEvent(Base):
    __tablename__ = "moderation_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    measurement_id: Mapped[str] = mapped_column(ForeignKey("measurements.id"), index=True)
    action: Mapped[str] = mapped_column(String(16))  # report | remove | restore | purge
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64))  # admin id or "public_report"
    created_utc: Mapped[str] = mapped_column(String(20))
    notice: Mapped[str | None] = mapped_column(Text)


class TemplateBankRecord(Base):
    """An administrator-uploaded template bank. Versions are immutable and never deleted (replay)."""

    __tablename__ = "template_banks"
    bank_id: Mapped[str] = mapped_column(String(82), primary_key=True)
    family: Mapped[str] = mapped_column(String(8))
    bank_version: Mapped[str] = mapped_column(String(64))
    n_templates: Mapped[int] = mapped_column(Integer)
    license: Mapped[str] = mapped_column(Text)
    data_sha256: Mapped[str] = mapped_column(String(64))
    data_bytes: Mapped[int] = mapped_column(Integer)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    manifest_key: Mapped[str] = mapped_column(String(300))
    data_key: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by: Mapped[str] = mapped_column(ForeignKey("admins.id"))
    uploaded_utc: Mapped[str] = mapped_column(String(20))


class BankEvent(Base):
    __tablename__ = "bank_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    bank_id: Mapped[str] = mapped_column(ForeignKey("template_banks.bank_id"), index=True)
    action: Mapped[str] = mapped_column(String(16))  # upload | activate | deactivate
    actor: Mapped[str] = mapped_column(String(64))
    created_utc: Mapped[str] = mapped_column(String(20))


def _sqlite_immediate(engine: Engine) -> None:
    """SQLite: serialize writers with BEGIN IMMEDIATE so conditional updates are atomic and
    concurrent read-then-write transactions cannot fail with an upgrade deadlock."""

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _record):
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


# PostgreSQL advisory-lock keys (transaction scoped; see `serialize`).
MIGRATION_LOCK = 0x48565301
CLAIM_LOCK = 0x48565302
QUOTA_LOCK = 0x48565303


def lock_key(namespace: int, name: str) -> int:
    """A 64-bit advisory-lock key for one named resource (e.g. a contributor's quota)."""
    return (namespace << 32) | int.from_bytes(hashlib.sha256(name.encode()).digest()[:4], "big")


def serialize(session_or_conn, key: int) -> None:
    """Run the rest of this transaction one at a time across processes for `key`.

    SQLite already serializes writers (BEGIN IMMEDIATE). PostgreSQL's READ COMMITTED does not, so a
    count-then-update (the job concurrency ceiling, concurrent startup migrations) takes a
    transaction-scoped advisory lock there."""
    bind = session_or_conn.get_bind() if hasattr(session_or_conn, "get_bind") else session_or_conn
    if bind.dialect.name == "postgresql":
        session_or_conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def driver_url(url: str) -> str:
    """Hosted providers hand out `postgres://` or `postgresql://` URLs; SQLAlchemy maps those to
    psycopg2, but the installed driver is psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> Engine:
    url = driver_url(url)
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})
        _sqlite_immediate(engine)
        return engine
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(url: str):
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(url: str) -> str:
    """Apply forward migrations only. Downgrades are refused unless explicitly authorized."""
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    command.upgrade(alembic_config(url), "head")
    with make_engine(url).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def destructive_migrations_authorized() -> bool:
    return os.environ.get("HVSR_ALLOW_DESTRUCTIVE_MIGRATIONS") == "yes-i-have-a-verified-backup"
