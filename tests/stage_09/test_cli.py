"""The offline `hvsr` CLI runs the science without HTTP, credentials, or a database: analysis,
exact replay of published results, template-bank build/validate, and Monte Carlo experiments."""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

from stage9_support import synthetic_csv_gz

ROOT = Path(__file__).resolve().parents[2]
HVSR = Path(sys.executable).parent / "hvsr"
EXAMPLES = ROOT / "docs/examples"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HVSR_")}
    return subprocess.run(
        [str(HVSR), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=900, check=False
    )


def _example_recording(tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("export_examples", ROOT / "scripts/export_examples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    path = tmp_path / mod.FILENAME
    path.write_bytes(mod.recording_csv_gz())
    return path


@pytest.mark.acceptance("AC-S9-13")
def test_analyze_writes_a_complete_run_without_the_web_stack(tmp_path):
    rec = tmp_path / "rec.csv.gz"
    rec.write_bytes(synthetic_csv_gz(900, seed=31))
    r = run("analyze", str(rec), "--csv-layout", "elapsed_time", "--plots", "--out", "out", cwd=tmp_path)
    assert r.returncode == 0, r.stderr[-2000:]
    out = tmp_path / "out"
    for method in ("standard", "sdof"):
        doc = json.loads((out / f"analysis_{method}.json").read_text())
        assert doc["method"] == method and doc["selections"]
        assert (out / f"mean_spectrum_{method}.csv").stat().st_size > 0
    assert any(p.suffix == ".svg" for p in out.iterdir())
    probe = (
        "import sys; from hvsr_core import cli; "
        f"code = cli.main(['analyze', {str(rec)!r}, '--csv-layout', 'elapsed_time', '--out', 'again']); "
        "loaded = [m for m in ('fastapi', 'sqlalchemy', 'hvsr_web', 'boto3') if m in sys.modules]; "
        "print(code, loaded)"
    )
    p = subprocess.run(
        [sys.executable, "-c", probe], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert p.stdout.strip().splitlines()[-1] == "0 []", p.stdout[-500:] + p.stderr[-2000:]
    assert run("analyze", str(tmp_path / "missing.csv"), "--out", "x", cwd=tmp_path).returncode == 2
    request_schema = run("schema", "config", cwd=tmp_path)
    assert request_schema.returncode == 0 and "AnalysisRequest" in json.loads(request_schema.stdout)["title"]
    unknown = run("schema", "no-such-schema", cwd=tmp_path)
    assert (
        unknown.returncode == 2 and "invalid choice" in unknown.stderr and "Traceback" not in unknown.stderr
    )


@pytest.mark.acceptance("AC-S9-13")
def test_replay_reproduces_the_published_example_and_detects_a_change(tmp_path):
    rec = _example_recording(tmp_path)
    docs = [str(EXAMPLES / f"analysis_{m}.json") for m in ("standard", "sdof")]
    r = run("replay", *docs, "--recording", str(rec), "--csv-layout", "elapsed_time", cwd=tmp_path)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    report = json.loads(r.stdout)
    assert report["status"] == "reproduced" and report["input_files_match_published_sources"] is True

    doc = json.loads((EXAMPLES / "analysis_sdof.json").read_text())
    curve = doc["selections"][0]["mean_curve"]["hv_geometric_mean"]
    i = next(i for i, v in enumerate(curve) if v is not None)
    curve[i] *= 1.001
    tampered = tmp_path / "analysis_sdof.json"
    tampered.write_text(json.dumps(doc))
    r = run(
        "replay",
        docs[0],
        str(tampered),
        "--recording",
        str(rec),
        "--csv-layout",
        "elapsed_time",
        cwd=tmp_path,
    )
    assert r.returncode == 1
    assert json.loads(r.stdout)["status"] != "reproduced"


@pytest.mark.acceptance("AC-S9-13")
def test_banks_build_validate_and_reject_tampering(tmp_path):
    bank_id = "sdof-ko40-cli-test-v1"
    r = run(
        "banks", "build", "--family", "sdof", "--conditioning", "ko:40", "--bank-id", bank_id,
        "--license", "CC0-1.0", "--out", "banks", cwd=tmp_path,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr[-2000:]
    manifest = tmp_path / "banks" / f"{bank_id}.json"
    data = tmp_path / "banks" / f"{bank_id}.npz"
    assert run("banks", "validate", str(manifest), cwd=tmp_path).returncode == 0

    original = data.read_bytes()
    with zipfile.ZipFile(io.BytesIO(original)) as z:
        arrays = {n[:-4]: np.load(io.BytesIO(z.read(n)), allow_pickle=False) for n in z.namelist()}
    first = next(k for k, v in arrays.items() if v.dtype.kind == "f" and v.size)
    arrays[first] = arrays[first] * 1.01
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    for name, payload in (
        ("changed numbers", buf.getvalue()),
        ("truncated", original[: len(original) // 2]),
        ("pickled object", _pickled_npz()),
    ):
        data.write_bytes(payload)
        r = run("banks", "validate", str(manifest), cwd=tmp_path)
        assert r.returncode in (1, 2), (name, r.stdout[-500:])
    data.write_bytes(original)
    assert run("banks", "validate", str(manifest), cwd=tmp_path).returncode == 0


def _pickled_npz() -> bytes:
    buf = io.BytesIO()
    np.savez(buf, templates=np.array([{"x": 1}], dtype=object))
    return buf.getvalue()


@pytest.mark.acceptance("AC-S9-13")
def test_monte_carlo_development_run_is_labelled_exploratory(tmp_path):
    r = run("mc", "--n", "2", "--n-truth", "2", "--scenarios", "sdof_2hz", "--out", "mc", cwd=tmp_path)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "exploratory" in r.stdout and "not the locked validation" in r.stdout
    summary = json.loads((tmp_path / "mc/dev-summary.json").read_text())
    assert summary["set"] == "dev" and "does not calibrate any interval" in summary["statement"]
    assert summary["replicates_per_scenario"] == {"sdof_2hz": 2}
    with gzip.open(tmp_path / "mc/dev-replicates.json.gz", "rt") as fh:
        assert len(json.load(fh)) >= 2
