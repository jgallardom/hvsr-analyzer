"""Administrator template-bank upload and activation: malformed banks are rejected with a clear
reason, nothing uploaded is executed, versions are immutable and kept for replay."""

from __future__ import annotations

import io
import json
import os
import zipfile

import numpy as np
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from hvsr_core import templates
from hvsr_core.config import AnalysisRequest
from hvsr_core.jsonutil import sha256_bytes, strict_loads
from hvsr_core.replay import replay_published
from hvsr_web import banks, worker
from hvsr_web.analysis_jobs import load_recording
from hvsr_web.api import create_app
from hvsr_web.db import Artifact, BankEvent, Base, Measurement, alembic_config

from stage9_support import make_ctx, new_admin, new_contributor, submit_synthetic

BANK_ID = "sdof-ko40-lab-v2"


@pytest.fixture(autouse=True)
def _isolated_bank_dirs(monkeypatch):
    monkeypatch.setenv("HVSR_BANK_DIRS", "")


def valid_bank(bank_id: str = BANK_ID, license_text: str = "CC-BY-4.0 (Stage 9 Lab)") -> tuple[dict, bytes]:
    manifest, data = templates.generate_sdof_bank(
        templates.ko_conditioning(40.0), bank_id=bank_id, bank_version="2.0.0"
    )
    manifest["license"] = license_text
    return manifest, data


def arrays_of(data: bytes) -> dict[str, np.ndarray]:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def npz(arrays: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    return buf.getvalue()


def rehash(manifest: dict, data: bytes) -> dict:
    return dict(manifest, data_sha256=sha256_bytes(data))


class Exploit:
    def __init__(self, marker: str):
        self.marker = marker

    def __reduce__(self):
        return (os.system, (f"touch {self.marker}",))


def malformed_cases(tmp_path) -> list[tuple[str, bytes, bytes, str]]:
    m, d = valid_bank()
    a = arrays_of(d)
    marker = tmp_path / "pickle-executed"
    pickled = io.BytesIO()
    np.save(pickled, np.array([Exploit(str(marker))], dtype=object), allow_pickle=True)
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as zf:
        for k, v in a.items():
            if k != "anchor_ratio":
                buf = io.BytesIO()
                np.save(buf, v)
                zf.writestr(f"{k}.npy", buf.getvalue())
        zf.writestr("anchor_ratio.npy", pickled.getvalue())
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as zf:
        zf.writestr("../x.npy", b"\x93NUMPY")
    nan_t = dict(a, log_templates=np.where(np.arange(a["log_templates"].size).reshape(a["log_templates"].shape) == 5,
                                            np.nan, a["log_templates"]))  # fmt: skip
    shifted = dict(a, log_templates=a["log_templates"] + 0.5)
    short = dict(a, x=a["x"][:-1], log_templates=a["log_templates"][:, :-1])
    extra = dict(a, payload=np.zeros(3))
    as_text = dict(a, anchor_ratio=np.array(["1.0"] * a["anchor_ratio"].size))
    j = lambda x: json.dumps(x).encode()  # noqa: E731
    cases = [
        ("not JSON", b"{not json", d, "not strict JSON"),
        ("NaN literal", j(m).replace(b'"n_templates": 9', b'"n_templates": NaN'), d, "not strict JSON"),
        ("schema: unknown field", j(dict(m, run_me="os.system")), d, "manifest invalid"),
        ("schema: missing field", j({k: v for k, v in m.items() if k != "grid"}), d, "manifest invalid"),
        ("hash mismatch", j(m), d[:-1] + bytes([d[-1] ^ 1]), "SHA-256 does not match"),
        ("not an archive", j(rehash(m, b"plain bytes")), b"plain bytes", "not a valid NPZ"),
        ("pickled object", j(rehash(m, evil.getvalue())), evil.getvalue(), "rejected"),
        ("path traversal", j(rehash(m, traversal.getvalue())), traversal.getvalue(), "unsafe"),
        ("extra array", j(rehash(m, npz(extra))), npz(extra), "unexpected array"),
        ("text dtype", j(rehash(m, npz(as_text))), npz(as_text), "non-numeric dtype"),
        ("wrong grid", j(rehash(m, npz(short))), npz(short), "129-point"),
        ("non-finite", j(rehash(m, npz(nan_t))), npz(nan_t), "non-finite"),
        ("not normalized", j(rehash(m, npz(shifted))), npz(shifted), "not normalized"),
        ("count mismatch", j(dict(m, n_templates=m["n_templates"] + 1)), d, "shapes inconsistent"),
        ("license", j(dict(m, license="Proprietary, all rights reserved")), d, "license 'Proprietary,'"),
        ("data file name", j(dict(m, data_file="other.npz")), d, "data_file must be"),
        ("shipped ID", j(dict(m, bank_id="sdof-raw-v1", data_file="sdof-raw-v1.npz")), d, "shipped bank ID"),
    ]
    return cases, marker


@pytest.mark.acceptance("AC-S9-02")
def test_malformed_banks_are_rejected_with_a_reason_and_never_executed(tmp_path):
    ctx = make_ctx(tmp_path)
    token, _ = new_admin(ctx)
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {token}"}
    cases, marker = malformed_cases(tmp_path)
    for name, manifest, data, reason in cases:
        r = client.post(
            "/api/v1/admin/banks",
            headers=auth,
            files={"manifest": ("m.json", manifest), "data": ("d.npz", data)},
        )
        assert r.status_code == 422, (name, r.status_code, r.text)
        body = r.json()["detail"]
        assert body["error"] == "bank_rejected" and reason in body["detail"], (name, body)
    assert not marker.exists()  # the pickled payload was never unpickled
    with ctx.sessions() as session:
        assert session.scalars(select(BankEvent)).all() == []
    assert client.get("/api/v1/banks").json()["banks"] == [
        b for b in client.get("/api/v1/banks").json()["banks"] if b["source"] == "shipped"
    ]

    # size ceilings apply before parsing
    small = make_ctx(tmp_path, "small", max_bank_bytes=1024)
    stoken, _ = new_admin(small)
    m, d = valid_bank()
    r = TestClient(create_app(small)).post(
        "/api/v1/admin/banks",
        headers={"Authorization": f"Bearer {stoken}"},
        files={"manifest": ("m.json", json.dumps(m).encode()), "data": ("d.npz", d)},
    )
    assert r.status_code == 422 and "exceeds 1024 bytes" in r.json()["detail"]["detail"]


@pytest.mark.acceptance("AC-S9-02")
def test_only_administrators_upload_or_activate(tmp_path):
    ctx = make_ctx(tmp_path)
    upload_token, _ = new_contributor(ctx)
    client = TestClient(create_app(ctx))
    m, d = valid_bank()
    files = {"manifest": ("m.json", json.dumps(m).encode()), "data": ("d.npz", d)}
    assert client.post("/api/v1/admin/banks", files=files).status_code == 401
    r = client.post("/api/v1/admin/banks", files=files, headers={"Authorization": f"Bearer {upload_token}"})
    assert r.status_code == 401
    assert client.post(f"/api/v1/admin/banks/{BANK_ID}/activate").status_code == 401


@pytest.mark.acceptance("AC-S9-02")
def test_bank_lifecycle_upload_activate_use_deactivate_replay(tmp_path):
    ctx = make_ctx(tmp_path)
    token, admin = new_admin(ctx)
    _, c = new_contributor(ctx)
    auth = {"Authorization": f"Bearer {token}"}
    client = TestClient(create_app(ctx))
    m, d = valid_bank()
    files = {"manifest": ("m.json", json.dumps(m).encode()), "data": ("d.npz", d)}

    r = client.post("/api/v1/admin/banks", headers=auth, files=files)
    assert (
        r.status_code == 201 and r.json()["active"] is False and r.json()["data_sha256"] == m["data_sha256"]
    )
    assert client.post("/api/v1/admin/banks", headers=auth, files=files).status_code == 200  # idempotent
    changed = dict(m, bank_version="2.0.1")
    r = client.post(
        "/api/v1/admin/banks",
        headers=auth,
        files={"manifest": ("m.json", json.dumps(changed).encode()), "data": ("d.npz", d)},
    )
    assert r.status_code == 422 and "immutable" in r.json()["detail"]["detail"]

    sub = submit_synthetic(ctx, c, 600, seed=12)
    body = AnalysisRequest.model_validate(
        {"config": {"sdof": {"template_bank": BANK_ID}}, "robustness_check": False}
    )
    req = body.model_dump(mode="json")
    upload_headers = {"Authorization": f"Bearer {new_contributor(ctx)[0]}"}
    r = client.post(f"/api/v1/measurements/{sub.measurement_id}/analyses", json=req, headers=upload_headers)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "bank_unavailable"  # inactive

    configure = f"/measurements/{sub.measurement_id}/configure"
    assert BANK_ID not in client.get(configure).text
    assert client.post(f"/api/v1/admin/banks/{BANK_ID}/activate", headers=auth).json()["active"] is True
    assert BANK_ID in client.get(configure).text
    r = client.post(f"/api/v1/measurements/{sub.measurement_id}/analyses", json=req, headers=upload_headers)
    assert r.status_code == 202, r.text
    [res] = worker.drain(ctx)
    assert res["state"] == "succeeded", res
    with ctx.sessions() as session:
        arts = session.scalars(
            select(Artifact).where(
                Artifact.run_id == res["result"]["run_id"], Artifact.kind == "analysis_json"
            )
        ).all()
        docs = [strict_loads(ctx.store.get(a.blob_key)) for a in arts]
        mrow = session.get(Measurement, sub.measurement_id)
        sources = list(
            session.scalars(
                select(Artifact).where(Artifact.measurement_id == mrow.id, Artifact.run_id.is_(None))
            )
        )
    sdof_doc = next(x for x in docs if x["method"] == "sdof")
    assert sdof_doc["method_status"] == "computed" and sdof_doc["selections"]
    assert sdof_doc["provenance"]["template_bank"]["bank_id"] == BANK_ID
    assert sdof_doc["provenance"]["template_bank"]["data_sha256"] == m["data_sha256"]

    assert client.post(f"/api/v1/admin/banks/{BANK_ID}/deactivate", headers=auth).json()["active"] is False
    other = submit_synthetic(ctx, c, 600, seed=13)
    r = client.post(f"/api/v1/measurements/{other.measurement_id}/analyses", json=req, headers=upload_headers)
    assert r.status_code == 422  # not selectable for new analyses ...
    listed = {b["bank_id"]: b for b in client.get("/api/v1/banks").json()["banks"]}
    assert listed[BANK_ID]["active"] is False  # ... but the version is kept
    assert BANK_ID not in client.get(configure).text
    rec, _, _ = load_recording(ctx, mrow, sources)
    assert replay_published(rec, docs)["status"] == "reproduced"  # and old runs still replay

    with ctx.sessions() as session:
        actions = [e.action for e in session.scalars(select(BankEvent).order_by(BankEvent.created_utc))]
    assert sorted(actions) == ["activate", "deactivate", "upload"]
    assert all(e for e in actions) and admin.id


@pytest.mark.acceptance("AC-S9-02")
def test_bank_migration_is_additive_and_models_match_migrations(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite3'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0001")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO admins (id, token_sha256, private_name, active, created_utc) "
            "VALUES ('a1', 'digest', 'Existing Admin', 1, '2026-01-01T00:00:00Z')"
        )
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT private_name FROM admins").scalar_one() == "Existing Admin"
        assert compare_metadata(MigrationContext.configure(conn), Base.metadata) == []
    with pytest.raises(RuntimeError, match="Refusing a downgrade"):
        command.downgrade(cfg, "0001")
    assert banks.KEY_PREFIX.startswith("templates/")
