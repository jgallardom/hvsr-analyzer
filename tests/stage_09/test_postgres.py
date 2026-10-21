"""The deployment database (PostgreSQL) works end to end: hosted URLs, migrations that match the
models, concurrent startup, the job concurrency ceiling under racing workers, idempotent concurrent
submissions, and backup/restore between PostgreSQL and SQLite.

Uses `HVSR_TEST_POSTGRES_URL` when set (CI service container); otherwise starts a throwaway local
server with `pgserver` (a dev dependency)."""

from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from hvsr_core.config import AnalysisRequest
from hvsr_web import analysis_jobs, jobs, worker
from hvsr_web.api import create_app
from hvsr_web.backup import create_backup, restore_backup
from hvsr_web.db import Base, QuotaPeriod, alembic_config, driver_url, make_engine, migrate
from hvsr_web.submissions import UploadFile, submit

from stage9_support import (
    fresh_postgres_db,
    make_ctx,
    migrate_after,
    new_contributor,
    submission,
    submit_synthetic,
    synthetic_csv_gz,
)


@pytest.fixture(scope="module")
def pg_server(tmp_path_factory):
    url = os.environ.get("HVSR_TEST_POSTGRES_URL")
    if url:
        yield url
        return
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    try:
        yield srv.get_uri()
    finally:
        srv.cleanup()


def _head(url: str) -> str:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


@pytest.mark.acceptance("AC-S9-14")
def test_hosted_postgres_urls_use_the_installed_driver():
    for url in ("postgres://u:p@db.example:5432/hvsr", "postgresql://u:p@db.example/hvsr"):
        engine = make_engine(url)
        assert (engine.dialect.name, engine.dialect.driver) == ("postgresql", "psycopg")
    assert driver_url("postgresql+psycopg://u@h/d") == "postgresql+psycopg://u@h/d"
    assert driver_url("sqlite:///./var/x.sqlite3") == "sqlite:///./var/x.sqlite3"


@pytest.mark.acceptance("AC-S9-14")
def test_migrations_match_models_and_refuse_downgrade_on_postgres(pg_server):
    url = fresh_postgres_db(pg_server)
    assert url.startswith("postgresql://")
    assert migrate(url) == _head(url)
    assert migrate(url) == _head(url)  # idempotent
    with make_engine(url).connect() as conn:
        assert conn.dialect.name == "postgresql"
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    with pytest.raises(RuntimeError, match="Refusing a downgrade"):
        command.downgrade(alembic_config(url), "0001")


@pytest.mark.acceptance("AC-S9-14")
def test_concurrent_startup_migrations_on_postgres(pg_server):
    url = fresh_postgres_db(pg_server)
    n = 4
    mp = multiprocessing.get_context("spawn")
    with mp.Manager() as manager:
        barrier = manager.Barrier(n)
        with ProcessPoolExecutor(n, mp_context=mp) as pool:
            heads = list(pool.map(migrate_after, [url] * n, [barrier] * n))
    assert heads == [_head(url)] * n
    with make_engine(url).connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 1


def _database(backend: str, request, tmp_path) -> str:
    if backend == "sqlite":
        return f"sqlite:///{tmp_path / 'race.sqlite3'}"
    return fresh_postgres_db(request.getfixturevalue("pg_server"))


@pytest.mark.acceptance("AC-S9-14", "AC-S9-12")
@pytest.mark.parametrize("backend", ["postgresql", "sqlite"])
def test_racing_workers_respect_the_concurrency_ceiling(tmp_path, request, backend):
    ctx = make_ctx(tmp_path, database_url=_database(backend, request, tmp_path), max_concurrent_jobs=1)
    for i in range(8):
        jobs.enqueue(ctx, "noop", {"i": i}, f"pg-noop-{i:04d}")
    racers = 8
    for rnd in range(4):
        barrier = threading.Barrier(racers)

        def attempt(i: int, rnd=rnd, barrier=barrier):
            barrier.wait(timeout=60)
            return jobs.claim(ctx, f"w{rnd}-{i}")

        with ThreadPoolExecutor(racers) as pool:
            claims = [c for c in pool.map(attempt, range(racers)) if c is not None]
        assert len(claims) == 1, f"round {rnd}: {len(claims)} jobs running with a ceiling of 1"
        assert jobs.complete(ctx, claims[0], {"ok": True})


@pytest.mark.acceptance("AC-S9-14")
@pytest.mark.parametrize("backend", ["postgresql", "sqlite"])
def test_concurrent_identical_submissions_store_and_charge_once(tmp_path, request, backend):
    ctx = make_ctx(tmp_path, database_url=_database(backend, request, tmp_path))
    _, contributor = new_contributor(ctx)
    data = synthetic_csv_gz(300, seed=3)
    n = 4
    barrier = threading.Barrier(n)

    def go(_):
        barrier.wait(timeout=60)
        return submit(ctx, contributor, submission("pg-same-key-0001"), [UploadFile("rec.csv.gz", data)])

    with ThreadPoolExecutor(n) as pool:
        results = list(pool.map(go, range(n)))
    assert len({r.measurement_id for r in results}) == 1
    assert sum(r.newly_charged for r in results) == 1
    charged = sum(r.charged_logical_bytes for r in results)
    assert charged > 0
    with ctx.sessions() as session:
        [period] = session.scalars(select(QuotaPeriod).where(QuotaPeriod.contributor_id == contributor.id))
        assert period.committed_bytes == charged and period.reserved_bytes == 0


@pytest.mark.acceptance("AC-S9-14", "AC-S9-08")
def test_analysis_pages_and_backup_restore_across_postgres_and_sqlite(tmp_path, pg_server):
    ctx = make_ctx(tmp_path, database_url=fresh_postgres_db(pg_server))
    _, c = new_contributor(ctx)
    sub = submit_synthetic(ctx, c, 600, seed=5)
    out = analysis_jobs.request_analysis(ctx, sub.measurement_id, AnalysisRequest(), c.id)
    [res] = worker.drain(ctx, isolated=True)
    assert res["job_id"] == out["job_id"] and res["state"] == "succeeded", res
    run_id = res["result"]["run_id"]

    def serves(app_ctx) -> None:
        client = TestClient(create_app(app_ctx), raise_server_exceptions=False)
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get(f"/measurements/{sub.measurement_id}").status_code == 200
        assert client.get(f"/runs/{run_id}?method=sdof").status_code == 200
        manifest = client.get(f"/api/v1/measurements/{sub.measurement_id}/manifest").json()
        for entries in manifest["categories"].values():
            for e in entries:
                if e.get("url") and e.get("availability") == "available":
                    assert client.get(e["url"], follow_redirects=True).status_code == 200, e["url"]

    serves(ctx)
    create_backup(ctx, tmp_path / "from-postgres")
    to_pg = make_ctx(tmp_path, "to-pg", database_url=fresh_postgres_db(pg_server))
    assert restore_backup(to_pg, tmp_path / "from-postgres")["verified"]
    serves(to_pg)
    to_sqlite = make_ctx(tmp_path, "to-sqlite")
    assert restore_backup(to_sqlite, tmp_path / "from-postgres")["verified"]
    serves(to_sqlite)
    # and the other way: moving a single-host SQLite deployment onto PostgreSQL
    create_backup(to_sqlite, tmp_path / "from-sqlite")
    back_to_pg = make_ctx(tmp_path, "back-to-pg", database_url=fresh_postgres_db(pg_server))
    assert restore_backup(back_to_pg, tmp_path / "from-sqlite")["verified"]
    serves(back_to_pg)
