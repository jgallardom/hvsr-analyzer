"""The recorded dependency scan matches the current lockfile and is clean; a fresh static scan has
no medium/high findings and every low finding is justified in docs/security-scans.md."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/stage9"
DOC = ROOT / "docs/security-scans.md"


@pytest.mark.acceptance("AC-S9-03")
def test_recorded_dependency_scan_is_current_and_clean():
    summary = json.loads((EVIDENCE / "security-scan.json").read_text())
    audit = summary["pip_audit"]
    assert audit is not None, "pip-audit has never been recorded"
    lock = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    assert audit["uv_lock_sha256"] == lock, "uv.lock changed: rerun scripts/security_scan.py"
    assert set(audit["services"]) == {"pypi", "osv"}
    for service, res in audit["services"].items():
        assert res["vulnerabilities"] == [], (service, res["vulnerabilities"])
        assert res["skipped"] == [], (service, res["skipped"])
        assert res["dependencies_audited"] + len(audit["not_audited"]) == audit["requirements_pinned"]
        report = json.loads((EVIDENCE / f"pip-audit-{service}.json").read_text())
        assert not any(d.get("vulns") for d in report["dependencies"])
    for entry in audit["not_audited"]:
        assert re.search(r"sys_platform == '(win32|emscripten|darwin)'", entry), entry


@pytest.mark.acceptance("AC-S9-03")
def test_static_scan_has_no_serious_findings_and_low_findings_are_justified(tmp_path):
    out = tmp_path / "bandit.json"
    bandit = Path(sys.executable).parent / "bandit"
    targets = ["src", "packages/hvsr_core/src", "scripts"]
    subprocess.run([str(bandit), "-r", *targets, "-f", "json", "-o", str(out), "-q"], cwd=ROOT, check=False)
    report = json.loads(out.read_text())
    assert report["errors"] == []
    serious = [r for r in report["results"] if r["issue_severity"] in ("MEDIUM", "HIGH")]
    assert serious == [], [(r["test_id"], r["filename"], r["line_number"]) for r in serious]
    assert report["metrics"]["_totals"]["nosec"] == 0, "findings must be justified, not suppressed"

    justified = {}
    for line in DOC.read_text().split("### Justified low findings", 1)[1].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[1].startswith("`"):
            justified[cells[1].strip("`").split("`")[0]] = set(re.findall(r"B\d{3}", cells[0]))
    fresh = {(r["test_id"], r["filename"]) for r in report["results"]}
    unjustified = sorted((t, f) for t, f in fresh if t not in justified.get(f, set()))
    assert unjustified == [], f"add these to docs/security-scans.md: {unjustified}"

    recorded = json.loads((EVIDENCE / "bandit.json").read_text())
    assert {(f["test_id"], f["file"]) for f in recorded["findings"]} == fresh, (
        "bandit evidence is stale: rerun scripts/security_scan.py --offline"
    )
