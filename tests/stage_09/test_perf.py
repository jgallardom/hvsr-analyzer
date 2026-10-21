"""The stored performance report is complete, matches its markdown rendering and the current limits,
and its deterministic counts reproduce when the small case is profiled again."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hvsr_web.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "docs/evidence/stage9/performance.json"
SCRIPT = ROOT / "scripts/profile_runs.py"


def _script():
    spec = importlib.util.spec_from_file_location("profile_runs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _deterministic(case: dict, *, analysis_bytes: bool = True) -> dict:
    """Everything in a case that does not depend on timing, the machine, or random identifiers.

    Analysis documents record in their provenance whether the checkout had uncommitted changes
    (`git_dirty`), so `analysis_bytes=False` drops their byte count for runs in different states."""
    # figure footers print the random run identifier, so raster sizes vary by a few bytes
    skipped = {"plots"} if analysis_bytes else {"plots", "analysis_runs"}
    runs = {}
    for rid, r in case["runs"].items():
        runs[rid] = {
            **{k: v for k, v in r.items() if k not in ("artifact_bytes", "artifact_bytes_by_category")},
            "bytes_excluding_figures": {
                k: v for k, v in r["artifact_bytes_by_category"].items() if k not in skipped
            },
        }
    sub = case["submission"]
    return {
        "recording": case["recording"],
        "submission": {k: sub[k] for k in ("upload_bytes", "decoded_bytes", "logical_bytes", "stored_bytes")},
        "budget_estimate": case["budget_estimate"],
        "runs": runs,
        "warm_reused": case["warm_report_only"]["science_cached"],
        "cold_cached": case["cold"]["science_cached"],
    }


@pytest.mark.acceptance("AC-S9-01")
def test_report_is_complete_and_matches_markdown_and_limits():
    doc = json.loads(REPORT.read_text())
    hw = doc["hardware"]
    for key in ("cpu_model", "logical_cpus", "memory_bytes", "os", "python", "numpy", "blas"):
        assert hw[key], key
    assert hw["numeric_threads_per_process"] == 1
    mod = _script()
    assert [c["case"] for c in doc["cases"]] == list(mod.CASES)
    for c in doc["cases"]:
        assert c["recording"]["duration_s"] == mod.CASES[c["case"]]["duration_s"]
        for phase in ("cold", "warm_report_only"):
            p = c[phase]
            assert p["job_wall_s"] > 0 and p["process_cpu_s"] > 0 and p["peak_rss_bytes"] > 0
        assert c["submission"]["wall_s"] > 0 and c["submission"]["peak_rss_bytes"] > 0
        assert c["database_growth_bytes"] > 0
        assert c["cold"]["science_cached"] is False and c["warm_report_only"]["science_cached"] is True
        assert set(c["runs"]) == {"A", "B"}
        for r in c["runs"].values():
            assert r["candidate_windows"] > 0 and r["template_comparisons"] > 0
            assert r["artifact_bytes"] == sum(r["artifact_bytes_by_category"].values()) > 0
            assert r["bootstrap_replicates"] >= 0
            estimated = [s for v in r["ci_status"].values() for s in v if s != "ci_not_estimable"]
            assert (r["bootstrap_replicates"] > 0) == bool(estimated)
    # the markdown is a rendering of this evidence, not a separately edited document
    assert (ROOT / "docs/performance.md").read_text() == mod.markdown(doc)
    # budgets were argued against the limits that are actually configured by default
    assert doc["limits"] == Settings().public_limits(), "limits changed: rerun scripts/profile_runs.py"


@pytest.mark.acceptance("AC-S9-01")
def test_profiling_small_case_again_reproduces_the_deterministic_counts(tmp_path):
    out = tmp_path / "perf.json"
    env = dict(os.environ, MPLCONFIGDIR=str(tmp_path / "mpl"))
    subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", "small", "--out", str(out), "--no-markdown"],
        check=True,
        env=env,
        timeout=600,
        cwd=ROOT,
    )
    fresh_doc, stored_doc = json.loads(out.read_text()), json.loads(REPORT.read_text())
    fresh = fresh_doc["cases"][0]
    stored = next(c for c in stored_doc["cases"] if c["case"] == "small")
    same_state = fresh_doc["git_dirty"] == stored_doc["git_dirty"]
    assert _deterministic(fresh, analysis_bytes=same_state) == _deterministic(
        stored, analysis_bytes=same_state
    )
