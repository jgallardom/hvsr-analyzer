"""Administrator template-bank upload and activation (spec 14).

An upload is data only: a JSON manifest and an NPZ archive of numeric arrays. It is validated by
`hvsr_core.templates.load_bank_bytes` (JSON Schema, SHA-256 of the data, archive entry names and
sizes, `allow_pickle=False`, numeric dtypes, matrix dimensions, the fixed ln-q grid, normalization,
and numerical domains). This module adds size and license checks, ID rules, storage, and
activation. Nothing uploaded is ever executed or unpickled.

Bank versions are immutable: a bank ID is uploaded once (re-sending identical bytes is a no-op) and
records are never deleted, so runs that used a deactivated bank stay replayable. New analyses may
select shipped banks and active uploaded banks only.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select

from hvsr_core import templates
from hvsr_core.jsonutil import sha256_bytes, strict_loads

from .context import AppContext
from .db import BankEvent, TemplateBankRecord, utc_iso

MAX_MANIFEST_BYTES = 256 * 1024
KEY_PREFIX = "templates/banks"


class BankRejected(ValueError):
    pass


class BankUnavailable(ValueError):
    pass


def license_id(text: str) -> str:
    return text.split()[0] if text.strip() else ""


def allowed_licenses(ctx: AppContext) -> list[str]:
    return [x.strip() for x in ctx.settings.bank_licenses.split(",") if x.strip()]


def shipped_bank_ids() -> list[str]:
    return sorted(p.stem for p in templates.SHIPPED_DIR.glob("*.json"))


def _public(r: TemplateBankRecord) -> dict:
    return {
        "bank_id": r.bank_id,
        "family": r.family,
        "bank_version": r.bank_version,
        "n_templates": r.n_templates,
        "license": r.license,
        "data_sha256": r.data_sha256,
        "data_bytes": r.data_bytes,
        "manifest_sha256": r.manifest_sha256,
        "active": r.active,
        "source": "uploaded",
        "uploaded_utc": r.uploaded_utc,
    }


def validate_upload(ctx: AppContext, manifest_bytes: bytes, data: bytes) -> dict:
    """Return the parsed manifest of a valid bank or raise BankRejected with the reason."""
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise BankRejected(f"manifest exceeds {MAX_MANIFEST_BYTES} bytes")
    limit = min(ctx.settings.max_bank_bytes, templates.MAX_NPZ_BYTES)
    if len(data) > limit:
        raise BankRejected(f"bank data exceeds {limit} bytes (HVSR_MAX_BANK_BYTES)")
    try:
        manifest = strict_loads(manifest_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise BankRejected(f"manifest is not strict JSON: {exc}") from None
    if not isinstance(manifest, dict):
        raise BankRejected("manifest must be a JSON object")
    try:
        templates.load_bank_bytes(manifest, data)
    except templates.BankError as exc:
        raise BankRejected(str(exc)) from None
    bank_id = manifest["bank_id"]
    if manifest["data_file"] != f"{bank_id}.npz":
        raise BankRejected(f"data_file must be {bank_id}.npz (one data file per bank ID)")
    if bank_id in shipped_bank_ids():
        raise BankRejected(f"{bank_id} is a shipped bank ID; upload under a new bank ID")
    lic = license_id(manifest["license"])
    if lic not in allowed_licenses(ctx):
        raise BankRejected(
            f"license {lic or '(empty)'!r} is not accepted; allowed: {', '.join(allowed_licenses(ctx))}"
        )
    return manifest


def upload(ctx: AppContext, admin_id: str, manifest_bytes: bytes, data: bytes) -> tuple[dict, bool]:
    """Validate and store a bank (inactive). Returns (bank, created)."""
    manifest = validate_upload(ctx, manifest_bytes, data)
    bank_id, data_sha, manifest_sha = (
        manifest["bank_id"],
        manifest["data_sha256"],
        sha256_bytes(manifest_bytes),
    )
    with ctx.sessions() as session:
        existing = session.get(TemplateBankRecord, bank_id)
        if existing is not None:
            if existing.data_sha256 == data_sha and existing.manifest_sha256 == manifest_sha:
                return _public(existing), False
            raise BankRejected(
                f"{bank_id} already exists with different content; bank versions are immutable"
            )
    data_key = f"{KEY_PREFIX}/{bank_id}/{data_sha}.npz"
    manifest_key = f"{KEY_PREFIX}/{bank_id}/{manifest_sha}.json"
    ctx.store.put(data_key, data, "application/octet-stream")
    ctx.store.put(manifest_key, manifest_bytes, "application/json")
    now = utc_iso(ctx.clock())
    with ctx.sessions() as session, session.begin():
        if session.get(TemplateBankRecord, bank_id) is not None:
            raise BankRejected(f"{bank_id} was uploaded concurrently; retry to compare content")
        rec = TemplateBankRecord(
            bank_id=bank_id,
            family=manifest["family"],
            bank_version=str(manifest["bank_version"])[:64],
            n_templates=manifest["n_templates"],
            license=manifest["license"],
            data_sha256=data_sha,
            data_bytes=len(data),
            manifest_sha256=manifest_sha,
            manifest_key=manifest_key,
            data_key=data_key,
            active=False,
            uploaded_by=admin_id,
            uploaded_utc=now,
        )
        session.add(rec)
        session.flush()
        session.add(BankEvent(bank_id=bank_id, action="upload", actor=admin_id, created_utc=now))
        return _public(rec), True


def set_active(ctx: AppContext, admin_id: str, bank_id: str, active: bool) -> dict:
    now = utc_iso(ctx.clock())
    with ctx.sessions() as session, session.begin():
        rec = session.get(TemplateBankRecord, bank_id)
        if rec is None:
            raise KeyError(bank_id)
        if rec.active != active:
            rec.active = active
            action = "activate" if active else "deactivate"
            session.add(BankEvent(bank_id=bank_id, action=action, actor=admin_id, created_utc=now))
        return _public(rec)


def listing(ctx: AppContext) -> list[dict]:
    shipped = [
        {
            "bank_id": b,
            "source": "shipped",
            "active": True,
            "license": templates.get_bank(b).manifest["license"],
        }
        for b in shipped_bank_ids()
    ]
    with ctx.sessions() as session:
        uploaded = [
            _public(r)
            for r in session.scalars(select(TemplateBankRecord).order_by(TemplateBankRecord.bank_id))
        ]
    return shipped + uploaded


def selectable_ids(ctx: AppContext, family: str | None = None) -> list[str]:
    """Bank IDs new analyses may use: shipped banks and active uploaded banks."""
    shipped = [b for b in shipped_bank_ids() if family is None or b.startswith(f"{family}-")]
    q = select(TemplateBankRecord.bank_id).where(TemplateBankRecord.active.is_(True))
    if family is not None:
        q = q.where(TemplateBankRecord.family == family)
    with ctx.sessions() as session:
        return sorted({*shipped, *session.scalars(q)})


def check_selectable(ctx: AppContext, bank_ids: list[str]) -> None:
    wanted = [b for b in bank_ids if b != "auto"]
    if not wanted:
        return
    allowed = set(selectable_ids(ctx))
    missing = [b for b in wanted if b not in allowed]
    if missing:
        raise BankUnavailable(f"template bank(s) not available for new analyses: {', '.join(missing)}")


def _sha_file(p: Path) -> str | None:
    return sha256_bytes(p.read_bytes()) if p.is_file() else None


def _write_atomic(p: Path, data: bytes) -> None:
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def materialize(ctx: AppContext) -> Path | None:
    """Write every uploaded bank (active or not, for replay) to `bank_dir`, verified against its
    SHA-256, and register the directory in HVSR_BANK_DIRS for `hvsr_core.templates`."""
    with ctx.sessions() as session:
        records = list(session.scalars(select(TemplateBankRecord)))
    if not records:
        return None
    directory = Path(ctx.settings.bank_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for r in records:
        dp, jp = directory / f"{r.bank_id}.npz", directory / f"{r.bank_id}.json"
        if _sha_file(dp) == r.data_sha256 and _sha_file(jp) == r.manifest_sha256:
            continue
        data, manifest = ctx.store.get(r.data_key), ctx.store.get(r.manifest_key)
        if sha256_bytes(data) != r.data_sha256 or sha256_bytes(manifest) != r.manifest_sha256:
            raise RuntimeError(f"stored bank {r.bank_id} failed its SHA-256 check")
        _write_atomic(dp, data)
        _write_atomic(jp, manifest)
    dirs = [p for p in os.environ.get("HVSR_BANK_DIRS", "").split(os.pathsep) if p]
    if str(directory) not in dirs:
        os.environ["HVSR_BANK_DIRS"] = os.pathsep.join([*dirs, str(directory)])
    return directory
