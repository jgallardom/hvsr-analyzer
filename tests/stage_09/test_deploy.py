"""Deployment configuration: the Render blueprint against Render's published schema, its secrets and
limits against the settings model and the measured memory budget, the container entry point, the
CI workflow, and fresh-install and Docker startup smoke tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

from hvsr_web import admin
from hvsr_web.api import create_app
from hvsr_web.settings import Settings

from stage9_support import make_ctx

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(__file__).parent / "data"
BLUEPRINT = ROOT / "render.yaml"
WORKFLOW = ROOT / ".github/workflows/ci.yml"
EVIDENCE = ROOT / "docs/evidence/stage9"
# RAM per Render compute plan (https://render.com/docs/compute-plans, retrieved 2026-10-21), taken as
# decimal gigabytes, the smaller reading of "2 GB".
PLAN_RAM_BYTES = {"free": 512e6, "0.5c-512mb": 512e6, "1c-2g": 2e9, "2c-4g": 4e9, "2c-8g": 8e9, "4c-8g": 8e9}
SECRETISH = re.compile(r"SECRET|PASSWORD|TOKEN|SIGNING_KEY|ACCESS_KEY|DATABASE_URL|CREDENTIAL", re.I)
SITE_FIELDS = {"database_url", "object_store", "s3_bucket", "s3_endpoint_url", "s3_region", "url_signing_key"}


def _blueprint() -> dict:
    return yaml.safe_load(BLUEPRINT.read_text())


def _web(doc: dict) -> dict:
    [web] = [s for s in doc["services"] if s["type"] == "web"]
    return web


def _literals(web: dict) -> dict[str, str]:
    return {e["key"]: str(e["value"]) for e in web["envVars"] if "value" in e}


def _field(key: str) -> str:
    return key.removeprefix("HVSR_").lower()


@pytest.mark.acceptance("AC-S9-10")
def test_blueprint_validates_against_renders_published_schema():
    source = json.loads((DATA / "render.schema.source.json").read_text())
    raw = (DATA / "render.schema.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == source["sha256"]
    schema = json.loads(raw)
    validator = jsonschema.validators.validator_for(schema)(schema)
    errors = [f"{list(e.path)}: {e.message[:200]}" for e in validator.iter_errors(_blueprint())]
    assert errors == []
    # the schema is strict enough to catch the mistakes that matter here
    broken = _blueprint()
    _web(broken)["plan"] = "2gb"
    _web(broken)["envVars"].append({"key": "X", "sync": "maybe"})
    assert list(validator.iter_errors(broken))


@pytest.mark.acceptance("AC-S9-10")
def test_blueprint_takes_secrets_only_from_the_environment():
    doc = _blueprint()
    web = _web(doc)
    for e in web["envVars"]:
        sources = {"value", "fromDatabase", "generateValue", "sync", "fromService", "fromGroup"} & set(e)
        assert len(sources) == 1, e
        if SECRETISH.search(e.get("key", "")):
            assert "value" not in e, f"{e['key']} must be prompted, generated, or referenced, not written"
        if "sync" in e:
            assert e["sync"] is False
    text = BLUEPRINT.read_text()
    assert not re.search(r"://[^\s/@]+:[^\s/@]+@", text), "credentials embedded in a URL"
    assert not re.search(r"[A-Za-z0-9+/=_-]{32,}", text), "a long token-like literal"
    assert "HVSR_ADMIN_TOKEN" not in text and "UPLOAD_TOKEN" not in text
    db = {e["key"]: e for e in web["envVars"]}["HVSR_DATABASE_URL"]
    assert db["fromDatabase"] == {"name": doc["databases"][0]["name"], "property": "connectionString"}
    assert _literals(web)["HVSR_OBJECT_STORE"] == "s3", "measurements must not live on the ephemeral disk"
    # an expiring free database or a spinning-down free web service would lose data or jobs
    assert web["plan"] != "free" and doc["databases"][0]["plan"] != "free"


@pytest.mark.acceptance("AC-S9-10")
def test_blueprint_settings_are_real_and_valid(tmp_path):
    web = _web(_blueprint())
    env_keys = [e["key"] for e in web["envVars"] if e["key"].startswith("HVSR_")]
    unknown = [k for k in env_keys if _field(k) not in Settings.model_fields]
    assert unknown == [], "misspelt settings would be silently ignored"
    settings = Settings.from_env(_literals(web))
    assert settings.embedded_worker and settings.job_isolation == "process"
    assert settings.numeric_threads == 1 and settings.max_concurrent_uploads == 1
    health = TestClient(create_app(make_ctx(tmp_path))).get(web["healthCheckPath"])
    assert health.status_code == 200 and health.json()["status"] == "ready"
    cmd = shlex.split(web["preDeployCommand"])
    assert cmd[:3] == ["python", "-m", "hvsr_web.admin"]
    assert admin.build_parser().parse_args(cmd[3:]).fn is admin.cmd_migrate


@pytest.mark.acceptance("AC-S9-10")
def test_blueprint_plan_fits_the_measured_memory_budget():
    web = _web(_blueprint())
    lim = Settings.from_env(_literals(web))
    perf = json.loads((EVIDENCE / "performance.json").read_text())
    small = next(c for c in perf["cases"] if c["case"] == "small")

    def upload_bound(upload: float, decoded: float, samples: float) -> float:
        # raw request + decoded text and its string + float64 columns (time + 3 components)
        return upload + 2 * decoded + 32 * samples

    for c in perf["cases"]:
        s = c["submission"]
        bound = small["submission"]["peak_rss_bytes"] + upload_bound(
            s["upload_bytes"], s["decoded_bytes"], c["recording"]["samples_per_channel"]
        )
        assert s["peak_rss_bytes"] <= bound, (c["case"], s["peak_rss_bytes"], bound)

    hosted = json.loads((EVIDENCE / "performance-render-limits.json").read_text())
    for key in (k for k in _literals(web) if k.startswith("HVSR_") and _field(k) not in SITE_FIELDS):
        assert hosted["limits"][_field(key)] == lim.model_dump()[_field(key)], f"rerun for {key}"
    baseline = hosted["import_baseline_rss_bytes"]
    for c in hosted["cases"]:
        assert c["recording"]["samples_per_channel"] <= lim.max_samples_per_channel
        assert c["runs"]["A"]["candidate_windows"] <= lim.max_candidate_windows
        assert c["cold"]["peak_rss_bytes"] <= baseline + lim.job_memory_bytes, c["case"]
    assert any(c["runs"]["A"]["candidate_windows"] > 0.9 * lim.max_candidate_windows for c in hosted["cases"])

    web_process = baseline + lim.max_concurrent_uploads * upload_bound(
        lim.max_request_bytes, lim.max_decoded_bytes, lim.max_samples_per_channel
    )
    job_process = baseline + lim.job_memory_bytes
    assert lim.max_concurrent_jobs == 1
    assert web_process + job_process <= 0.95 * PLAN_RAM_BYTES[web["plan"]]


@pytest.mark.acceptance("AC-S9-10")
def test_env_example_lists_every_setting_with_its_default_and_no_secret_values():
    lines = [ln for ln in (ROOT / ".env.example").read_text().splitlines() if ln and not ln.startswith("#")]
    pairs = dict(ln.split("=", 1) for ln in lines)
    hvsr = {k: v for k, v in pairs.items() if k.startswith("HVSR_")}
    assert sorted(_field(k) for k in hvsr) == sorted(Settings.model_fields)
    for k, v in pairs.items():
        if SECRETISH.search(k) and k != "HVSR_DATABASE_URL":
            assert v == "", f"{k} must be empty in the example"
    # loaded as written (`set -a; . ./.env`), blank lines included, the example is exactly the defaults
    assert Settings.from_env(hvsr) == Settings()
    assert ".env" in (ROOT / ".gitignore").read_text().split()


@pytest.mark.acceptance("AC-S9-10")
def test_container_image_installs_only_locked_runtime_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text()
    py = (ROOT / ".python-version").read_text().strip()
    bases = re.findall(r"^FROM python:(\d+\.\d+)-", dockerfile, re.M)
    assert bases and set(bases) == {py}
    uv_tag = re.search(r"ghcr\.io/astral-sh/uv:(\S+) ", dockerfile).group(1)
    assert yaml.safe_load(WORKFLOW.read_text())["env"]["UV_VERSION"] == uv_tag
    syncs = re.findall(r"uv sync (.+)", dockerfile)
    assert syncs and all("--frozen" in s and "--no-default-groups" in s for s in syncs)
    assert re.search(r"^USER (?!root)\S+", dockerfile, re.M)
    assert 'ENTRYPOINT ["/usr/local/bin/hvsr-entrypoint"]' in dockerfile
    ignored = set((ROOT / ".dockerignore").read_text().split())
    assert {".git", ".venv", ".env", "var", "tests"} <= ignored


@pytest.mark.acceptance("AC-S9-10")
def test_entrypoint_runs_one_server_process_and_records_the_commit(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uvicorn").write_text('#!/bin/sh\necho "commit=$HVSR_GIT_COMMIT args=$*"\n')
    (stub / "uvicorn").chmod(0o755)
    entry = str(ROOT / "scripts/docker-entrypoint.sh")
    base = {"PATH": f"{stub}:/usr/bin:/bin", "PORT": "10000", "WEB_CONCURRENCY": "4"}

    def run(env, *cmd):
        return subprocess.run(["sh", entry, *cmd], env=env, capture_output=True, text=True, check=True).stdout

    out = run(base | {"RENDER_GIT_COMMIT": "abc123"})
    assert "commit=abc123" in out and "--workers 1" in out and "--port 10000" in out
    assert "--factory" in out and "hvsr_web.api:create_app" in out
    assert "commit=def456" in run(base | {"RENDER_GIT_COMMIT": "abc123", "HVSR_GIT_COMMIT": "def456"})
    assert run(base, "sh", "-c", 'echo "[$HVSR_GIT_COMMIT]"').strip() == "[]"


@pytest.mark.acceptance("AC-S9-06")
def test_ci_runs_the_same_single_gate_command_with_postgres_and_scans():
    wf = yaml.safe_load(WORKFLOW.read_text())
    triggers = wf.get("on", wf.get(True))
    assert {"push", "pull_request"} <= set(triggers)
    assert wf["permissions"] == {"contents": "read"}
    gate, security = wf["jobs"]["gate"], wf["jobs"]["security"]
    runs = [s.get("run", "") for s in gate["steps"]]
    assert "uv sync --frozen" in runs
    assert "uv run ruff check ." in runs and "uv run ruff format --check ." in runs
    assert "uv run python scripts/gate.py --all" in runs
    checkout = next(s for s in gate["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout.get("with", {}).get("fetch-depth") == 0, "the Stage 5 lock test needs full git history"
    pg = gate["services"]["postgres"]
    major = _blueprint()["databases"][0]["postgresMajorVersion"]
    assert pg["image"] == f"postgres:{major}", "CI must test the PostgreSQL major version deployed"
    assert gate["env"]["HVSR_TEST_POSTGRES_URL"].startswith("postgresql://")
    assert any("scripts/security_scan.py" in s.get("run", "") for s in security["steps"])
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+", step["uses"]), step["uses"]


@pytest.mark.acceptance("AC-S9-06")
def test_gate_counts_a_blocked_requirement_only_where_its_tests_ran_and_passed():
    spec = importlib.util.spec_from_file_location("gate", ROOT / "scripts/gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    reqs = [
        {"id": "AC-X", "stage": 9, "status": "BLOCKED"},
        {"id": "AC-Y", "stage": 9, "status": "UNVALIDATED"},
    ]

    def states(results):
        rows, problems = gate.evaluate(reqs, results, 9)
        assert not problems
        return {r["id"]: r["state"] for r in rows}

    assert states({}) == {"AC-X": "BLOCKED", "AC-Y": "UNVALIDATED"}
    assert states({"AC-X": [("t", "skipped")], "AC-Y": [("t", "passed")]}) == {
        "AC-X": "BLOCKED",
        "AC-Y": "UNVALIDATED",
    }
    assert states({"AC-X": [("t", "passed"), ("u", "failed")]})["AC-X"] == "BLOCKED"
    assert states({"AC-X": [("t", "passed")]})["AC-X"] == "PASS"


@pytest.mark.acceptance("AC-S9-11")
def test_fresh_runtime_install_starts_serves_and_analyzes(tmp_path):
    report = tmp_path / "smoke.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/smoke.py"), "local", "--clean-install", "--report", str(report)],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    )  # fmt: skip
    assert report.exists(), proc.stderr[-3000:]
    doc = json.loads(report.read_text())
    failed = [c for c in doc["checks"] if not c["ok"]]
    assert proc.returncode == 0 and doc["ok"] and not failed, (failed, doc.get("server_log_tail", "")[-2000:])
    names = {c["check"] for c in doc["checks"]}
    assert {
        "runtime environment has no dev or reference packages",
        "pre-deploy migrate",
        "readyz (database and object store)",
        "blueprint limits in effect",
        "analysis job succeeded (embedded worker)",
        "graceful stop on SIGTERM",
    } <= names


@pytest.mark.acceptance("AC-S9-09")
def test_docker_image_builds_and_passes_the_smoke_test(tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is not installed here; AC-S9-09 stays BLOCKED until it runs (CI has Docker)")
    image = os.environ.get("HVSR_SMOKE_IMAGE")
    if image is None:
        image = "hvsr-analyzer:gate"
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
        subprocess.run(
            [docker, "build", "--tag", image, "--build-arg", f"GIT_COMMIT={commit.strip()}", "."],
            cwd=ROOT, check=True, timeout=3600,
        )  # fmt: skip
    report = tmp_path / "smoke.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/smoke.py"), "docker", "--image", image, "--report", str(report)],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    )  # fmt: skip
    doc = json.loads(report.read_text())
    assert proc.returncode == 0 and doc["ok"], [c for c in doc["checks"] if not c["ok"]]
    assert "runs as a non-root user" in {c["check"] for c in doc["checks"]}
