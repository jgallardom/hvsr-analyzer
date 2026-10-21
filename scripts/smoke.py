"""Startup smoke test of a real server: health, readiness, pages, and one complete upload ->
analysis -> published run over HTTP, with the job worker embedded in the server process.

    uv run python scripts/smoke.py local                    # this checkout's environment
    uv run python scripts/smoke.py local --clean-install    # a fresh runtime-only environment from uv.lock
    uv run python scripts/smoke.py docker --image hvsr-analyzer
    uv run python scripts/smoke.py url https://hvsr.example.org [--blueprint-limits]   # read-only

`local` and `docker` run the server with the operational limits declared for the web service in
render.yaml; only the database and object store are replaced by a temporary SQLite database and
local directory. They run the blueprint's pre-deploy command (`admin migrate`), issue an upload
token with the admin CLI, and stop the server with SIGTERM as a platform would. A JSON report goes
to stdout (and to --report FILE); the exit status is 0 only when every check passed.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
# deployment-specific values that the smoke site replaces with a temporary local site
SITE_KEYS = {
    "HVSR_DATABASE_URL",
    "HVSR_OBJECT_STORE",
    "HVSR_S3_BUCKET",
    "HVSR_S3_ENDPOINT_URL",
    "HVSR_S3_REGION",
    "HVSR_URL_SIGNING_KEY",
    "HVSR_PUBLIC_BASE_URL",
    "HVSR_MODERATION_CONTACT",
}
DEV_ONLY_MODULES = ("pytest", "ruff", "moto", "bandit", "pip_audit", "pgserver", "hvsrpy")


def blueprint_limits(path: Path = ROOT / "render.yaml") -> dict[str, str]:
    """Literal HVSR_* values of the web service in the Render blueprint (operational limits only)."""
    import yaml

    doc = yaml.safe_load(path.read_text())
    [web] = [s for s in doc["services"] if s["type"] == "web"]
    return {
        e["key"]: str(e["value"])
        for e in web["envVars"]
        if "value" in e and e["key"].startswith("HVSR_") and e["key"] not in SITE_KEYS
    }


def site_env(base: str) -> dict[str, str]:
    return blueprint_limits() | {
        "HVSR_DATABASE_URL": f"sqlite:///{base}/hvsr.sqlite3",
        "HVSR_OBJECT_STORE": "local",
        "HVSR_LOCAL_STORE_DIR": f"{base}/objects",
        "HVSR_BANK_DIR": f"{base}/banks",
        "HVSR_URL_SIGNING_KEY": secrets.token_hex(32),
        "HVSR_WORKER_POLL_S": "0.5",
    }


def base_env(tmp: Path) -> dict[str, str]:
    """A clean process environment: no HVSR_* settings leak in from the caller."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "SYSTEMROOT", "UV_CACHE_DIR", "UV_OFFLINE")
    return {k: os.environ[k] for k in keep if k in os.environ} | {
        "MPLCONFIGDIR": str(tmp / "mpl"),
        "TMPDIR": str(tmp),
        "PYTHONUNBUFFERED": "1",
    }


def free_port() -> int:
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != 8765:  # reserved for another local application
            return port
    raise RuntimeError("no free port")


def synthetic_csv_gz(duration_s: float = 600.0, fs: float = 50.0, seed: int = 7) -> bytes:
    from hvsr_core.research.synthetic import TFSpec, generate_3c

    rec, _ = generate_3c(duration_s=duration_s, fs=fs, tf=TFSpec("sdof", {"fn": 2.0, "zeta": 0.2}), seed=seed)
    data = rec.segments[0].data
    lines = ["time_s,N,E,Z\n"]
    lines += [
        f"{i / fs:.3f},{data[0, i]:.6f},{data[1, i]:.6f},{data[2, i]:.6f}\n" for i in range(data.shape[1])
    ]
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write("".join(lines).encode())
    return out.getvalue()


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, detail: object = "") -> bool:
        self.items.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(c["ok"] for c in self.items)


def wait_until_up(client: httpx.Client, timeout_s: float, alive=lambda: True) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and alive():
        try:
            if client.get("/healthz").status_code == 200:
                return True
        except httpx.TransportError:
            pass
        time.sleep(0.25)
    return False


def exercise(base_url: str, checks: Checks, token: str | None, expected_limits: dict | None) -> None:
    """Read-only checks; with an upload token also one submission, analysis, and published run."""
    with httpx.Client(base_url=base_url, timeout=60, follow_redirects=True) as c:
        r = c.get("/healthz")
        body = r.json() if r.status_code == 200 else {}
        checks.add("healthz", body.get("status") == "ok" and body.get("version"), body or r.status_code)
        r = c.get("/readyz")
        checks.add("readyz (database and object store)", r.status_code == 200, r.json())
        r = c.get("/")
        checks.add(
            "home page", r.status_code == 200 and "text/html" in r.headers["content-type"], r.status_code
        )
        r = c.get("/api/v1/limits")
        limits = r.json().get("limits", {}) if r.status_code == 200 else {}
        checks.add("limits endpoint", "max_request_bytes" in limits, r.status_code)
        if expected_limits:
            served = {k: str(limits.get(k.removeprefix("HVSR_").lower())) for k in expected_limits}
            wrong = {k: [v, served[k]] for k, v in expected_limits.items() if served[k].lower() != v.lower()}
            checks.add("blueprint limits in effect", not wrong, wrong or f"{len(served)} values match")
        if token is None:
            return

        from hvsr_core.metadata import MeasurementMetadata
        from hvsr_web.licensing import ConsentForm
        from hvsr_web.submissions import SubmissionRequest, WaveformOptions

        req = SubmissionRequest(
            idempotency_key="smoke-" + secrets.token_hex(6),
            metadata=MeasurementMetadata(latitude=40.4168, longitude=-3.7038),
            consent=ConsentForm(consent_checked=True, rights_confirmed=True, attribution="Smoke test"),
            waveform=WaveformOptions(csv_layout="elapsed_time"),
        )
        auth = {"Authorization": f"Bearer {token}"}
        r = c.post(
            "/api/v1/submissions",
            headers=auth,
            data={"submission": req.model_dump_json()},
            files=[("files", ("smoke.csv.gz", synthetic_csv_gz()))],
        )
        if not checks.add("submission accepted", r.status_code == 201, r.text[:300]):
            return
        mid = r.json()["measurement_id"]
        checks.add("measurement page", c.get(f"/measurements/{mid}").status_code == 200)
        r = c.post(f"/api/v1/measurements/{mid}/analyses", headers=auth, json={})
        if not checks.add("analysis queued", r.status_code == 202, r.text[:300]):
            return
        job_url, deadline, job = r.json()["job_url"], time.monotonic() + 300, {}
        while time.monotonic() < deadline:
            job = c.get(job_url).json()
            if job["state"] in ("succeeded", "dead"):
                break
            time.sleep(0.5)
        if not checks.add("analysis job succeeded (embedded worker)", job.get("state") == "succeeded", job):
            return
        run_id = job["result"]["run_id"]
        r = c.get(f"/api/v1/runs/{run_id}")
        checks.add("published run", r.status_code == 200 and r.json()["run_id"] == run_id, r.status_code)
        checks.add("run page", c.get(f"/runs/{run_id}").status_code == 200)


def graceful(checks: Checks, log: str, exit_ok: bool, detail: str) -> None:
    """uvicorn finishes the lifespan (stopping the embedded worker) and then re-raises the SIGTERM it
    caught, so a graceful stop is the shutdown log lines plus an exit by that signal (or 0)."""
    done = "Application shutdown complete." in log and "Finished server process" in log
    checks.add("graceful stop on SIGTERM", done and exit_ok, f"{detail}; shutdown logged: {done}")


def _admin(cmd: list[str], *args: str, env: dict | None = None) -> dict:
    out = subprocess.run([*cmd, *args], env=env, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({out.returncode}): {out.stderr.strip()}")
    return json.loads(out.stdout)


def clean_environment(venv: Path, env: dict) -> Path:
    """Install only the locked runtime dependencies (no dev or reference groups) into `venv`."""
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for --clean-install")
    subprocess.run(
        [uv, "sync", "--frozen", "--no-default-groups", "--no-editable"],
        cwd=ROOT, env=env | {"UV_PROJECT_ENVIRONMENT": str(venv)}, check=True, timeout=1200,
    )  # fmt: skip
    return venv / "bin" / "python"


def run_local(args, checks: Checks, report: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="hvsr-smoke-") as tmp_name:
        tmp = Path(tmp_name)
        env = base_env(tmp)
        python = Path(sys.executable)
        if args.clean_install:
            t0 = time.monotonic()
            python = clean_environment(tmp / "venv", env)
            report["clean_install_s"] = round(time.monotonic() - t0, 1)
            probe = "import importlib.util as u, sys; print([m for m in sys.argv[1:] if u.find_spec(m)])"
            found = subprocess.run(
                [python, "-c", probe, *DEV_ONLY_MODULES], capture_output=True, text=True, check=True
            ).stdout.strip()
            checks.add("runtime environment has no dev or reference packages", found == "[]", found)
        report["python"] = str(python)
        env |= site_env(str(tmp / "site"))
        admin = [str(python), "-m", "hvsr_web.admin"]
        checks.add("pre-deploy migrate", "schema_revision" in _admin(admin, "migrate", env=env))
        token = _admin(admin, "issue-upload-token", "--note", "smoke test", env=env)["token"]
        port = args.port or free_port()
        log = tmp / "server.log"
        with log.open("wb") as fh:
            proc = subprocess.Popen(
                [str(python), "-m", "uvicorn", "hvsr_web.api:create_app", "--factory",
                 "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
                env=env, stdout=fh, stderr=subprocess.STDOUT,
            )  # fmt: skip
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}") as c:
                    up = wait_until_up(c, 120, alive=lambda: proc.poll() is None)
                if checks.add("server started", up, f"port {port}"):
                    exercise(f"http://127.0.0.1:{port}", checks, token, blueprint_limits())
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                try:
                    code = proc.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = proc.wait()
        text = log.read_text(errors="replace")
        graceful(checks, text, code in (0, -signal.SIGTERM), f"exit code {code}")
        if not checks.ok:
            report["server_log_tail"] = text[-4000:]


def run_docker(args, checks: Checks, report: dict) -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker is not installed")
    name = "hvsr-smoke-" + secrets.token_hex(4)
    port = args.port or free_port()
    env = [f"--env={k}={v}" for k, v in site_env("/data").items()]
    subprocess.run(
        [docker, "run", "--detach", "--name", name, "--publish", f"127.0.0.1:{port}:8000", *env, args.image],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    report["image"] = args.image
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as c:
            up = wait_until_up(c, 120)
        if checks.add("container started", up, f"port {port}"):
            uid = subprocess.run([docker, "exec", name, "id", "-u"], capture_output=True, text=True).stdout
            checks.add("runs as a non-root user", uid.strip() not in ("", "0"), uid.strip())
            admin = [docker, "exec", name, "python", "-m", "hvsr_web.admin"]
            checks.add("pre-deploy migrate", "schema_revision" in _admin(admin, "migrate"))
            token = _admin(admin, "issue-upload-token", "--note", "smoke test")["token"]
            exercise(f"http://127.0.0.1:{port}", checks, token, blueprint_limits())
        subprocess.run([docker, "stop", "--time", "90", name], check=True, capture_output=True, timeout=150)
        code = subprocess.run(
            [docker, "inspect", "--format", "{{.State.ExitCode}}", name], capture_output=True, text=True
        ).stdout.strip()
        logs = subprocess.run([docker, "logs", name], capture_output=True, text=True)
        text = logs.stdout + logs.stderr
        graceful(checks, text, code in ("0", str(128 + signal.SIGTERM)), f"exit code {code}")
        if not checks.ok:
            report["server_log_tail"] = text[-4000:]
    finally:
        subprocess.run([docker, "rm", "--force", name], capture_output=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--report", type=Path, help="also write the JSON report here")
    sub = ap.add_subparsers(dest="mode", required=True)
    loc = sub.add_parser("local", parents=[common])
    loc.add_argument("--clean-install", action="store_true")
    loc.add_argument("--port", type=int)
    dock = sub.add_parser("docker", parents=[common])
    dock.add_argument("--image", required=True)
    dock.add_argument("--port", type=int)
    url = sub.add_parser("url", parents=[common])
    url.add_argument("base_url")
    url.add_argument(
        "--blueprint-limits", action="store_true", help="also check that the render.yaml limits are in effect"
    )
    args = ap.parse_args()

    checks = Checks()
    report: dict = {"mode": args.mode, "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        if args.mode == "local":
            report["clean_install"] = args.clean_install
            run_local(args, checks, report)
        elif args.mode == "docker":
            run_docker(args, checks, report)
        else:
            report["url"] = args.base_url
            exercise(
                args.base_url.rstrip("/"), checks, None, blueprint_limits() if args.blueprint_limits else None
            )
    except (RuntimeError, OSError, subprocess.SubprocessError, httpx.HTTPError, ValueError) as exc:
        checks.add("smoke run completed", False, f"{type(exc).__name__}: {exc}")
    report |= {"ok": checks.ok, "checks": checks.items}
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n")
    return 0 if checks.ok else 1


if __name__ == "__main__":
    sys.exit(main())
