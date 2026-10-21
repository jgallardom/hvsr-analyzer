"""Dependency vulnerability scan (pip-audit) and static security scan (bandit), recorded as evidence.

    uv run python scripts/security_scan.py             # both scans; pip-audit needs network access
    uv run python scripts/security_scan.py --offline   # bandit only; keeps the last pip-audit evidence

pip-audit checks every package pinned in `uv.lock` (all dependency groups, with hashes) against the
PyPI advisory data, and the OSV database as a second source. bandit scans shipped code and scripts.
Results go to docs/evidence/stage9/ together with the SHA-256 of `uv.lock`, so the AC-S9-03 test can
tell whether the recorded dependency scan still describes the current lockfile. Exit status 1 means
a vulnerability or a medium/high bandit finding was reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/evidence/stage9"
BANDIT_TARGETS = ["src", "packages/hvsr_core/src", "scripts"]
SERVICES = ("pypi", "osv")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False, **kw)


def _version(tool: str) -> str:
    return _run([str(Path(sys.executable).parent / tool), "--version"]).stdout.strip().splitlines()[0]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_sha256() -> str:
    return hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()


def run_pip_audit(tmp: Path) -> dict:
    req = tmp / "requirements.txt"
    exp = _run(
        ["uv", "export", "--frozen", "--format", "requirements-txt", "--all-groups", "--no-emit-workspace"]
    )
    if exp.returncode != 0:
        raise SystemExit(f"uv export failed: {exp.stderr.strip()}")
    req.write_text(exp.stdout)
    services = {}
    for service in SERVICES:
        out = tmp / f"pip-audit-{service}.json"
        r = _run(
            [str(Path(sys.executable).parent / "pip-audit"), "-r", str(req), "--require-hashes", "--disable-pip",
             "-s", service, "--cache-dir", str(tmp / "cache"), "-f", "json", "-o", str(out),
             "--progress-spinner", "off"],
            timeout=900,
        )  # fmt: skip
        if not out.exists():
            raise SystemExit(f"pip-audit ({service}) did not run: {(r.stderr or r.stdout).strip()[-600:]}")
        report = json.loads(out.read_text())
        (OUT / f"pip-audit-{service}.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
        deps = report["dependencies"]
        services[service] = {
            "dependencies_audited": sum(1 for d in deps if "skip_reason" not in d),
            "skipped": sorted(f"{d['name']}: {d['skip_reason']}" for d in deps if "skip_reason" in d),
            "vulnerabilities": sorted(
                f"{d['name']} {d['version']}: {v['id']} (fixed in {', '.join(v['fix_versions']) or 'no release'})"
                for d in deps
                for v in d.get("vulns", [])
            ),
        }
    pinned = {}
    for ln in exp.stdout.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^ ;\\]+)(.*)$", ln)
        if m:
            pinned[_norm(m.group(1))] = m.group(3).strip(" \\").lstrip("; ")
    audited = {_norm(d["name"]) for d in report["dependencies"]}
    return {
        "tool": _version("pip-audit"),
        "scope": "every package pinned in uv.lock, all dependency groups, hashes required",
        "requirements_pinned": len(pinned),
        "not_audited": sorted(
            f"{n} (environment marker: {pinned[n] or 'none'})" for n in set(pinned) - audited
        ),
        "uv_lock_sha256": lock_sha256(),
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "services": services,
    }


def run_bandit(tmp: Path) -> dict:
    out = tmp / "bandit.json"
    _run(
        [
            str(Path(sys.executable).parent / "bandit"),
            "-r",
            *BANDIT_TARGETS,
            "-f",
            "json",
            "-o",
            str(out),
            "-q",
        ],
        timeout=600,
    )
    report = json.loads(out.read_text())
    findings = sorted(
        (r["issue_severity"], r["issue_confidence"], r["test_id"], r["filename"], r["line_number"])
        for r in report["results"]
    )
    totals = report["metrics"]["_totals"]
    stable = {
        "tool": _version("bandit"),
        "targets": BANDIT_TARGETS,
        "lines_scanned": totals["loc"],
        "nosec": totals["nosec"],
        "by_severity": {s: totals[f"SEVERITY.{s}"] for s in ("HIGH", "MEDIUM", "LOW")},
        "findings": [
            {"severity": s, "confidence": c, "test_id": t, "file": f, "line": n} for s, c, t, f, n in findings
        ],
        "errors": report["errors"],
    }
    (OUT / "bandit.json").write_text(json.dumps(stable, indent=1) + "\n")
    return stable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="skip pip-audit (needs network)")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    summary_path = OUT / "security-scan.json"
    previous = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    with tempfile.TemporaryDirectory() as tmp:
        bandit = run_bandit(Path(tmp))
        audit = previous.get("pip_audit") if args.offline else run_pip_audit(Path(tmp))
    summary = {
        "bandit": {k: v for k, v in bandit.items() if k != "findings"}
        | {"generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
        "pip_audit": audit,
    }
    summary_path.write_text(json.dumps(summary, indent=1) + "\n")
    vulns = [v for s in (audit or {}).get("services", {}).values() for v in s["vulnerabilities"]]
    serious = bandit["by_severity"]["HIGH"] + bandit["by_severity"]["MEDIUM"]
    print(json.dumps({"bandit": bandit["by_severity"], "vulnerabilities": vulns}, indent=1))
    if audit is None:
        print(
            "pip-audit has never been recorded; run without --offline where network is available",
            file=sys.stderr,
        )
        return 1
    if audit["uv_lock_sha256"] != lock_sha256():
        print("uv.lock changed since the recorded pip-audit; rerun without --offline", file=sys.stderr)
    return 1 if vulns or serious else 0


if __name__ == "__main__":
    sys.exit(main())
