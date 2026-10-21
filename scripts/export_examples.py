#!/usr/bin/env python3
"""Write concrete example exports (spec 13.3) to docs/examples/ from a real analysis.

A throwaway local site (temporary database and object store) receives a deterministic synthetic
recording through the HTTP API and analyzes it; every file is saved exactly as downloaded from its
stable artifact URL. IDs, timestamps, and record codes therefore belong to that throwaway site.
`tests/stage_08/test_examples.py` validates the files and checks their numbers against a fresh
analysis of the same recording. The per-window spectra (about 1.8 MB compressed for this record)
and the normalized waveform are left out to keep the repository small; both validate in the tests.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import secrets
import shutil
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_web import worker
from hvsr_web.api import create_app
from hvsr_web.auth import issue_upload_token
from hvsr_web.context import AppContext
from hvsr_web.settings import Settings
from hvsr_web.storage import LocalObjectStore

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "examples"

# The recording: 1800 s at 50 Hz of the synthetic SDOF response (fn 2 Hz, damping 0.2), seed 21,
# written as an elapsed-time CSV with 6 decimals. Long enough for the nominal 95% band.
RECORDING = {"duration_s": 1800, "fs": 50, "fn": 2.0, "zeta": 0.2, "seed": 21}
SUBMISSION = {
    "idempotency_key": "docs-example-0001",
    "waveform": {"csv_layout": "elapsed_time"},
    "metadata": {"latitude": 40.416775, "longitude": -3.70379},
    "consent": {"consent_checked": True, "rights_confirmed": True, "attribution": "Example Observatory"},
}
REQUEST = {"robustness_check": False}
FILENAME = "synthetic_sdof2hz_1800s.csv.gz"


def recording_csv_gz() -> bytes:
    r = RECORDING
    rec, _ = generate_3c(
        duration_s=r["duration_s"],
        fs=r["fs"],
        tf=TFSpec("sdof", {"fn": r["fn"], "zeta": r["zeta"]}),
        seed=r["seed"],
    )
    data = rec.segments[0].data
    lines = ["time_s,N,E,Z\n"]
    for i in range(data.shape[1]):
        lines.append(f"{i / r['fs']:.2f},{data[0, i]:.6f},{data[1, i]:.6f},{data[2, i]:.6f}\n")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write("".join(lines).encode())
    return buf.getvalue()


def _get(client: TestClient, ctx: AppContext, url: str) -> bytes:
    r = client.get(url)
    if r.status_code == 202:
        worker.drain(ctx)
        r = client.get(url)
    if r.status_code != 200:
        raise SystemExit(f"{url}: HTTP {r.status_code} {r.text[:200]}")
    return r.content


def export(out: Path) -> list[Path]:
    work = Path(tempfile.mkdtemp(prefix="hvsr-examples-"))
    try:
        key = secrets.token_hex(32)
        s = Settings(
            database_url=f"sqlite:///{work / 'db.sqlite3'}",
            local_store_dir=str(work / "objects"),
            url_signing_key=key,
        )
        ctx = AppContext.from_settings(s, store=LocalObjectStore(s.local_store_dir, key.encode()))
        with ctx.sessions() as session, session.begin():
            token, _ = issue_upload_token(session, private_email="examples@example.org")
        client = TestClient(create_app(ctx))
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post(
            "/api/v1/submissions",
            data={"submission": json.dumps(SUBMISSION), "roles": ["waveform"]},
            files=[("files", (FILENAME, recording_csv_gz()))],
            headers=auth,
        )
        if r.status_code != 201:
            raise SystemExit(f"submission: HTTP {r.status_code} {r.text[:300]}")
        mid = r.json()["measurement_id"]
        r = client.post(f"/api/v1/measurements/{mid}/analyses", json=REQUEST, headers=auth)
        if r.status_code != 202:
            raise SystemExit(f"analysis: HTTP {r.status_code} {r.text[:300]}")
        worker.drain(ctx)
        job = client.get(r.json()["job_url"]).json()
        if job["state"] != "succeeded":
            raise SystemExit(f"analysis job: {job}")

        manifest_url = f"/api/v1/measurements/{mid}/manifest"
        manifest = json.loads(_get(client, ctx, manifest_url))
        wanted = {
            ("analysis_runs", "analysis_json", "sdof"): "analysis_sdof.json",
            ("analysis_runs", "analysis_json", "standard"): "analysis_standard.json",
            ("analysis_runs", "window_table_csv", "sdof"): "window_table_sdof.csv",
            ("analysis_runs", "window_table_csv", "standard"): "window_table_standard.csv",
            ("spectra", "mean_spectrum_csv", "sdof"): "mean_spectrum_sdof.csv",
            ("spectra", "mean_spectrum_csv", "standard"): "mean_spectrum_standard.csv",
            ("plots", "plot_svg", "sdof"): "plot_sdof.svg",
        }
        out.mkdir(parents=True, exist_ok=True)
        written = []
        for (category, kind, method), name in wanted.items():
            found = [
                e
                for e in manifest["categories"][category]
                if e["kind"] == kind and e.get("method") == method and e["availability"] != "unavailable"
            ]
            if not found:
                raise SystemExit(f"no {category}/{kind}/{method} artifact in the manifest")
            (out / name).write_bytes(_get(client, ctx, found[0]["url"]))
            written.append(out / name)
        (out / "manifest.json").write_bytes(_get(client, ctx, manifest_url))
        (out / "data_dictionary.json").write_bytes(_get(client, ctx, "/api/v1/data-dictionary"))
        return [*written, out / "manifest.json", out / "data_dictionary.json"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    for p in export(args.out):
        print(f"{p.stat().st_size:>10,}  {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}")


if __name__ == "__main__":
    main()
