"""Stage 5: Monte Carlo harness mechanics, protocol lock, and honest reporting of the locked test set."""

from __future__ import annotations

import tomllib
from pathlib import Path

import numpy as np
import pytest

from hvsr_core.config import AnalysisConfig
from hvsr_core.jsonutil import sha256_bytes, strict_loads
from hvsr_core.research import validation_protocol as vp
from hvsr_core.research.validation import (
    SCENARIOS,
    make_record,
    pool_truth,
    replicate,
    seeds,
    truth_contribution,
)

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "docs/validation/protocol-v1.json"
LOCK = ROOT / "docs/validation/protocol-v1.lock.json"
SUMMARY = ROOT / "artifacts/validation/test-v1-summary.json"
REPORT = ROOT / "docs/validation/report-v1.md"


def _strip_times(d):
    if isinstance(d, dict):
        return {k: _strip_times(v) for k, v in d.items() if k not in ("times", "uncertainty_s")}
    if isinstance(d, list):
        return [_strip_times(v) for v in d]
    return d


# --------------------------------------------------------------------------- AC-S5-01
@pytest.mark.acceptance("AC-S5-01")
def test_seed_sets_are_disjoint():
    all_sets = {}
    for i, _ in enumerate(SCENARIOS):
        for name in ("dev", "test", "truth"):
            s = set(seeds(name, i, vp.REPLICATES[name]))
            for key, other in all_sets.items():
                assert not (s & other), (name, i, key)
            all_sets[(name, i)] = s


@pytest.mark.acceptance("AC-S5-01")
def test_harness_is_reproducible_from_seeds():
    sc = next(s for s in SCENARIOS if s.name == "regime_switching")
    a, _ = make_record(sc, seeds("dev", 6, 1)[0])
    b, _ = make_record(sc, seeds("dev", 6, 1)[0])
    np.testing.assert_array_equal(a.segments[0].data, b.segments[0].data)
    cfg = AnalysisConfig()
    truth = pool_truth([truth_contribution(sc, s, cfg) for s in seeds("truth", 6, 3)])
    r1 = replicate(sc, seeds("dev", 6, 1)[0], cfg, truth)
    r2 = replicate(sc, seeds("dev", 6, 1)[0], cfg, truth)
    assert _strip_times(r1) == _strip_times(r2)


# --------------------------------------------------------------------------- AC-S5-02
@pytest.mark.acceptance("AC-S5-02")
def test_protocol_is_hash_locked_and_committed_before_the_test_set():
    lock = strict_loads(LOCK.read_text())
    assert lock["protocol_sha256"] == sha256_bytes(PROTOCOL.read_bytes())
    for p in (PROTOCOL, LOCK):
        vp.git("ls-files", "--error-unmatch", str(p.relative_to(ROOT)), cwd=ROOT)
    lock_commit = vp.git(
        "log", "--diff-filter=A", "-n", "1", "--format=%H", "--", str(LOCK.relative_to(ROOT)), cwd=ROOT
    )
    proto_commit = vp.git(
        "log", "--diff-filter=A", "-n", "1", "--format=%H", "--", str(PROTOCOL.relative_to(ROOT)), cwd=ROOT
    )
    vp.git("merge-base", "--is-ancestor", proto_commit, lock_commit, cwd=ROOT)
    summ = strict_loads(SUMMARY.read_text())
    assert summ["set"] == "test"
    assert summ["protocol_sha256"] == lock["protocol_sha256"]
    vp.git("merge-base", "--is-ancestor", lock_commit, summ["head_commit"], cwd=ROOT)
    added = vp.git(
        "log", "--diff-filter=A", "-n", "1", "--format=%H", "--", str(SUMMARY.relative_to(ROOT)), cwd=ROOT
    )
    if added:  # once results are committed, they must come after the lock
        vp.git("merge-base", "--is-ancestor", lock_commit, added, cwd=ROOT)
        assert added != lock_commit


@pytest.mark.acceptance("AC-S5-02")
def test_check_lock_refuses_modified_protocol(tmp_path):
    bad = tmp_path / "protocol-v1.json"
    bad.write_bytes(PROTOCOL.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="hash differs"):
        vp.check_lock(tmp_path, bad, LOCK)
    with pytest.raises(RuntimeError, match="no lock file"):
        vp.check_lock(tmp_path, PROTOCOL, tmp_path / "missing.lock.json")


@pytest.mark.acceptance("AC-S5-02")
def test_protocol_predeclares_counts_seeds_metrics_and_targets():
    p = strict_loads(PROTOCOL.read_text())
    assert p["sets"]["test"]["replicates_per_scenario"] == vp.REPLICATES["test"]
    assert {"dev", "test", "truth"} <= set(p["sets"])
    assert p["metrics"] and p["targets"] and p["decision_rule"]
    assert all("rule" in t and t["alpha"] > 0 for t in p["targets"])
    assert p["code_fingerprint"]["files"]


# --------------------------------------------------------------------------- AC-S5-03
@pytest.mark.acceptance("AC-S5-03")
def test_scenarios_cover_required_conditions():
    p = strict_loads(PROTOCOL.read_text())
    cats = {c for s in p["scenarios"] for c in s["categories"]}
    required = {
        "flat",
        "broad",
        "asymmetric",
        "multiple_peaks",
        "weak_fundamental",
        "colored_noise",
        "temporal_correlation",
        "transients",
        "machine_tone",
        "component_mismatch",
        "layered_site_response",
    }
    assert required <= cats, required - cats
    assert any("non_sdof" in s["categories"] for s in p["scenarios"])


# --------------------------------------------------------------------------- AC-S5-04
METRICS = (
    "coverage_policy_mean",
    "coverage_physical_amplitude",
    "ci_width_ln",
    "no_result_rate",
    "fp_ln_error",
    "fp_abs_ln_error",
    "fp_abs_rel_error",
    "windows",
    "informative_blocks",
    "uncertainty_s",
)


@pytest.mark.acceptance("AC-S5-04")
def test_report_has_all_metrics_for_both_methods():
    summ = strict_loads(SUMMARY.read_text())
    assert set(summ["summary"]) == {s.name for s in SCENARIOS}
    for name, s in summ["summary"].items():
        assert s["n_replicates"] == vp.REPLICATES["test"]
        assert {"false_peak", "fundamental_correct"} <= set(s["discovery"])
        assert {"core_s", "sdof_match_s"} <= set(s["compute_s"])
        for m in ("standard", "sdof"):
            missing = [k for k in METRICS if k not in s[m]]
            assert not missing, (name, m, missing)
    text = REPORT.read_text()
    assert "Standard" in text and "SDOF" in text and "verdict" in text


# --------------------------------------------------------------------------- AC-S5-05
@pytest.mark.acceptance("AC-S5-05")
def test_r2_vs_fundamental_cases_are_reported():
    summ = strict_loads(SUMMARY.read_text())
    for s in summ["summary"].values():
        assert {"r2_preferred_family_wrong", "sdof_r2_up_fp_worse", "inferred_fundamental_wrong"} <= set(
            s["r2_vs_fundamental"]
        )
    multi = [
        summ["summary"][n]["r2_vs_fundamental"]["r2_preferred_family_wrong"]
        for n in ("two_peaks", "weak_fundamental")
    ]
    assert sum(m["n"] for m in multi) > 0  # applicable cases exist and are counted, whatever their rate
    assert "R2 vs fundamental" in REPORT.read_text()


# --------------------------------------------------------------------------- AC-S5-06
@pytest.mark.acceptance("AC-S5-06")
def test_validation_status_is_consistent_with_published_verdict():
    summ = strict_loads(SUMMARY.read_text())
    req = next(
        r
        for r in tomllib.loads((ROOT / "docs/acceptance/requirements.toml").read_text())["req"]
        if r["id"] == "AC-S5-06"
    )
    report = REPORT.read_text()
    failed = [t["id"] for t in summ["targets"] if t["result"] != "PASS"]
    if summ["verdict"] == "met":
        assert not failed and req.get("status") is None and "MET" in report
    else:
        assert failed
        assert req.get("status") == "UNVALIDATED", (
            "a failed scientific target must be published as UNVALIDATED"
        )
        assert "NOT MET" in report
        assert all(t in req["reason"] for t in failed[:3])
