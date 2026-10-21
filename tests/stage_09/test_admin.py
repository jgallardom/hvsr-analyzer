"""Operator commands (`python -m hvsr_web.admin`) work as documented in docs/backup-restore.md and
docs/template-banks.md: migrate, tokens, backup create/verify/restore, bank upload/activate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hvsr_core import templates
from hvsr_web.context import AppContext
from hvsr_web.db import Measurement
from hvsr_web.settings import Settings

from stage9_support import SIGNING_KEY, new_contributor, submit_synthetic

ROOT = Path(__file__).resolve().parents[2]


def site_env(tmp_path: Path, name: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("HVSR_")}
    return base | {
        "HVSR_DATABASE_URL": f"sqlite:///{tmp_path / name}.sqlite3",
        "HVSR_LOCAL_STORE_DIR": str(tmp_path / f"{name}-objects"),
        "HVSR_BANK_DIR": str(tmp_path / f"{name}-banks"),
        "HVSR_URL_SIGNING_KEY": SIGNING_KEY,
    }


def admin(env: dict, *args: str) -> tuple[int, dict | list | None, str]:
    p = subprocess.run(
        [sys.executable, "-m", "hvsr_web.admin", *args],
        env=env, cwd=ROOT, capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip
    return p.returncode, (json.loads(p.stdout) if p.returncode == 0 else None), p.stderr


@pytest.mark.acceptance("AC-S9-08")
def test_backup_and_restore_through_the_operator_commands(tmp_path):
    env = site_env(tmp_path, "live")
    code, out, err = admin(env, "migrate")
    assert code == 0, err
    head = out["schema_revision"]
    ctx = AppContext.from_settings(Settings.from_env(env))
    _, c = new_contributor(ctx)
    sub = submit_synthetic(ctx, c, 300, seed=8)

    bk = tmp_path / "backups" / "hvsr-20261021"
    code, manifest, err = admin(env, "backup", "create", str(bk))
    assert code == 0, err
    assert manifest["schema_revision"] == head and manifest["tables"]["measurements"]["rows"] == 1
    assert admin(env, "backup", "verify", str(bk))[0] == 0

    target = site_env(tmp_path, "restored")
    assert admin(target, "migrate")[0] == 0
    code, report, err = admin(target, "backup", "restore", str(bk))
    assert code == 0 and report["verified"] is True, err
    restored = AppContext.from_settings(Settings.from_env(target))
    with restored.sessions() as session:
        assert session.scalar(select(Measurement.id)) == sub.measurement_id
    code, _, err = admin(target, "backup", "restore", str(bk))
    assert code == 1 and "empty database" in err  # never merges into or overwrites data

    some_object = next(p for p in (bk / "objects").rglob("*") if p.is_file())
    some_object.write_bytes(some_object.read_bytes() + b"x")
    code, _, err = admin(env, "backup", "verify", str(bk))
    assert code == 1 and "checksum mismatch" in err
    fresh = site_env(tmp_path, "fresh")
    assert admin(fresh, "migrate")[0] == 0
    assert admin(fresh, "backup", "restore", str(bk))[0] == 1
    with AppContext.from_settings(Settings.from_env(fresh)).sessions() as session:
        assert session.scalar(select(func.count()).select_from(Measurement)) == 0


@pytest.mark.acceptance("AC-S9-02")
def test_bank_commands_need_an_admin_token_from_the_environment(tmp_path):
    env = site_env(tmp_path, "site")
    assert admin(env, "migrate")[0] == 0
    code, issued, err = admin(env, "issue-admin-token", "--name", "Operator")
    assert code == 0, err
    code, upload, _ = admin(env, "issue-upload-token", "--email", "someone@example.org")
    assert code == 0 and upload["token"] != issued["token"]

    manifest, data = templates.generate_sdof_bank(
        templates.ko_conditioning(40.0), bank_id="sdof-ko40-ops-v1", bank_version="1.0.0",
        created_utc="2026-10-21T00:00:00Z",
    )  # fmt: skip
    manifest["license"] = "CC0-1.0"
    folder = tmp_path / "bank"
    folder.mkdir()
    (folder / "sdof-ko40-ops-v1.json").write_text(json.dumps(manifest))
    (folder / manifest["data_file"]).write_bytes(data)
    mpath = str(folder / "sdof-ko40-ops-v1.json")

    assert admin(env, "banks", "upload", mpath)[0] == 1  # no token
    assert admin(env | {"HVSR_ADMIN_TOKEN": upload["token"]}, "banks", "upload", mpath)[0] == 1
    as_admin = env | {"HVSR_ADMIN_TOKEN": issued["token"]}
    code, bank, err = admin(as_admin, "banks", "upload", mpath)
    assert code == 0 and bank["created"] is True and bank["active"] is False, err
    code, bank, err = admin(as_admin, "banks", "activate", "sdof-ko40-ops-v1")
    assert code == 0 and bank["active"] is True, err
    code, listing, _ = admin(env, "banks", "list")
    uploaded = [b for b in listing if b["source"] == "uploaded"]
    assert [(b["bank_id"], b["active"]) for b in uploaded] == [("sdof-ko40-ops-v1", True)]
