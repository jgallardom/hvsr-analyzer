"""Protocol, hash lock, parallel runner and predeclared target evaluation for mc-harness-v1."""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.stats import binomtest

from .. import versions
from ..config import AnalysisConfig
from ..jsonutil import canonical_dumps, pretty_dumps, sha256_bytes, strict_loads
from .synthetic import GENERATOR_VERSION
from .validation import (
    DURATION_S,
    FS_HZ,
    HARNESS_VERSION,
    SCENARIOS,
    SET_BASE,
    pool_truth,
    replicate,
    seeds,
    truth_contribution,
)

PROTOCOL_ID = "hvsr-mc-validation-v1"
REPLICATES = {"dev": 50, "test": 400, "truth": 200}
COVERAGE_TARGET = 0.95
NULL_FALSE_PEAK_MAX = 0.10
FAMILY_ALPHA = 0.05
MIN_EVALUABLE = 30
PKG = Path(__file__).resolve().parents[1]
VALIDATED_CONFIG_SECTIONS = (
    "preprocessing", "windowing", "spectral", "peak_search", "basic_qc", "standard_selection",
    "peak_discovery", "sdof", "uncertainty",
)  # fmt: skip
FINGERPRINT_FILES = (
    "core.py", "curves.py", "matching.py", "peaks.py", "preprocess.py", "qc.py", "selection.py",
    "spectra.py", "templates.py", "uncertainty.py", "windows.py", "metadata.py",
    "research/synthetic.py", "research/validation.py", "research/validation_protocol.py",
)  # fmt: skip


def code_fingerprint() -> dict:
    files = {f: sha256_bytes((PKG / f).read_bytes()) for f in FINGERPRINT_FILES}
    banks = {p.name: sha256_bytes(p.read_bytes()) for p in sorted((PKG / "banks").glob("sdof-*.json"))}
    return {"files": files, "bank_manifests": banks}


def targets() -> list[dict]:
    out = []
    for sc in SCENARIOS:
        out.append(
            {"id": f"T-COV-STD-{sc.name}", "kind": "coverage", "method": "standard", "scenario": sc.name}
        )
    for sc in SCENARIOS:
        if not sc.null:
            out.append(
                {"id": f"T-COV-SDOF-{sc.name}", "kind": "coverage", "method": "sdof", "scenario": sc.name}
            )
    for sc in SCENARIOS:
        if sc.null:
            out.append(
                {
                    "id": f"T-FALSEPEAK-{sc.name}",
                    "kind": "null_false_peak",
                    "method": "discovery",
                    "scenario": sc.name,
                }
            )
    m = len(out)
    for t in out:
        t["alpha"] = FAMILY_ALPHA / m
        if t["kind"] == "coverage":
            t["rule"] = (
                f"one-sided exact binomial test of H0: coverage >= {COVERAGE_TARGET} on replicates whose CI is estimated or "
                f"limited; FAIL if p < {FAMILY_ALPHA}/{m}; NOT EVALUABLE (counts as not met) with < {MIN_EVALUABLE} such replicates. "
                "Over-coverage passes and is reported."
            )
        else:
            t["rule"] = (
                f"one-sided exact binomial test of H0: false-peak rate <= {NULL_FALSE_PEAK_MAX}; FAIL if p < {FAMILY_ALPHA}/{m}."
            )
    return out


def build_protocol() -> dict:
    cfg = AnalysisConfig()
    return {
        "protocol_id": PROTOCOL_ID,
        "harness_version": HARNESS_VERSION,
        "generator_version": GENERATOR_VERSION,
        "package_version": versions.PACKAGE_VERSION,
        "algorithm_versions": dict(versions.ALGORITHM_VERSIONS),
        "record": {"duration_s": DURATION_S, "sampling_rate_hz": FS_HZ},
        "analysis_config": {
            k: v for k, v in cfg.model_dump(mode="json").items() if k in VALIDATED_CONFIG_SECTIONS
        },
        "scenarios": [sc.describe() for sc in SCENARIOS],
        "sets": {
            k: {
                "replicates_per_scenario": REPLICATES[k],
                "seed_rule": f"{SET_BASE[k]} + 10000 * scenario_index + i",
            }
            for k in ("dev", "test", "truth")
        },
        "estimands": {
            "policy_mean": "expected ln H/V at the coverage frequency over windows retained by the method's policy; estimated by pooling retained windows of all truth records",
            "standard_policy": "basic QC + earliest-first non-overlapping windows (Standard reference)",
            "sdof_policy": "SDOF R2_log greedy selection inside the plausible family matching the true fundamental (selected-regime mean)",
            "physical": "ln|T(f)| of the generating transfer function at the coverage frequency (RMS-combined with any declared horizontal gain mismatch) and the true fundamental frequency; not a CI target",
        },
        "matching_rules": {
            "family_matches_true_peak": "true peak inside [lo_hz, hi_hz] or |log10(mean_peak_hz / true)| <= 0.1",
            "fundamental_correct": "|log10(inferred fundamental / true fundamental)| <= 0.1",
            "false_peak": "plausible family without possible_source_peak flag that matches no true transfer-function peak",
        },
        "metrics": [
            "bias of ln(fp/f0); median |ln(fp/f0)|; mean |fp - f0| / f0 (Standard and SDOF separately)",
            "fundamental-correct rate; false-peak rate; source-flag rate",
            "pointwise CI coverage of the policy mean (target) and of the physical amplitude (reported)",
            "CI width in ln at the coverage frequency",
            "no-result rates: ci_not_estimable, ci_limited, no SDOF selection, no fp",
            "retained evidence: windows, informative blocks",
            "peak-replicate conditional interval coverage of f0 (reported, conditional, not a 95 % claim)",
            "compute cost per replicate",
            "R2-vs-fundamental: R2-preferred family wrong; SDOF R2 above Standard windows while |fp error| worse",
        ],
        "targets": targets(),
        "development_log": [
            "dev run 1 (50 replicates/scenario, development seeds only) showed: (a) default STA/LTA rejects 96-100 % of "
            "windows in the alpha = 1.5 coloured-noise scenarios, so no CI is produced (kept unchanged; reported as a QC "
            "limitation); (b) the transient scenario with 40 bursts per 30 min left about 10 clean windows, contrary to "
            "the spec's 'clean windows mixed with strong transients'; reduced to 12 bursts; (c) coverage 43/49 in "
            "flat_white, consistent with plain-percentile narrowness for k = 18 blocks; the expanded percentile interval "
            "(D-052) was adopted; (d) regime_switching under-covered (24/33, 27/37); the method was NOT changed for this.",
            "added colored_mild_sdof (alpha = 0.75) and episodes_lognormal (white noise with amplitude episodes) so CI "
            "behaviour under coloured noise and under episodes can be evaluated at all; the harsh variants are kept.",
            "dev run 2 (after the changes above): regime_switching still under-covers (26/33, 29/37) and the alpha = 1.5 "
            "scenarios still produce no CI; both are expected to fail on the test set and no further change was made.",
            "no locked-test-set seed was used before the lock.",
        ],
        "decision_rule": "AC-S5-06 is met only if every target passes. Otherwise the failure is published and the CI remains labelled nominal/uncalibrated.",
        "code_fingerprint": code_fingerprint(),
    }


def protocol_bytes(p: dict) -> bytes:
    return pretty_dumps(p).encode("utf-8")


def git(*args: str, cwd: Path) -> str:
    exe = shutil.which("git")
    if exe is None:
        raise RuntimeError("git is required to verify the protocol lock")
    return subprocess.run([exe, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()  # noqa: S603


def check_lock(root: Path, protocol_path: Path, lock_path: Path) -> dict:
    """Refuse the locked test set unless the committed protocol equals the regenerated one and the lock hash."""
    if not lock_path.exists():
        raise RuntimeError(f"no lock file {lock_path}; run `validate_mc.py lock` and commit it first")
    lock = strict_loads(lock_path.read_text())
    data = protocol_path.read_bytes()
    h = sha256_bytes(data)
    if h != lock["protocol_sha256"]:
        raise RuntimeError("protocol file hash differs from the lock")
    if data != protocol_bytes(build_protocol()):
        raise RuntimeError(
            "code, configuration or scenarios changed since the protocol was written (fingerprint mismatch)"
        )
    for p in (protocol_path, lock_path):
        rel = str(p.relative_to(root))
        git("ls-files", "--error-unmatch", rel, cwd=root)
        if git("status", "--porcelain", "--", rel, cwd=root):
            raise RuntimeError(f"{rel} has uncommitted changes")
    return {
        "protocol_sha256": h,
        "lock_commit": git("log", "-n", "1", "--format=%H", "--", str(lock_path.relative_to(root)), cwd=root),
    }


def _truth_job(args):
    name, seed = args
    from .validation import SCENARIO_BY_NAME

    return name, truth_contribution(SCENARIO_BY_NAME[name], seed, AnalysisConfig())


def _rep_job(args):
    name, seed, truth = args
    from .validation import SCENARIO_BY_NAME

    return replicate(SCENARIO_BY_NAME[name], seed, AnalysisConfig(), truth)


def _pool():
    tmp = Path(tempfile.gettempdir())
    os.environ.setdefault("MPLCONFIGDIR", str(tmp / "hvsr-mpl"))
    os.environ.setdefault("NUMBA_CACHE_DIR", str(tmp / "hvsr-numba"))
    return ProcessPoolExecutor(max_workers=max(1, min(6, (os.cpu_count() or 2) - 1)))


def compute_truth(n_truth: int, scenario_names: list[str] | None = None) -> dict:
    jobs = []
    for i, sc in enumerate(SCENARIOS):
        if scenario_names and sc.name not in scenario_names:
            continue
        jobs += [(sc.name, s) for s in seeds("truth", i, n_truth)]
    parts: dict[str, list] = {}
    with _pool() as ex:
        for name, c in ex.map(_truth_job, jobs, chunksize=4):
            parts.setdefault(name, []).append(c)
    return {name: pool_truth(v) for name, v in parts.items()}


def run_set(set_name: str, n: int, truth: dict, scenario_names: list[str] | None = None) -> list[dict]:
    jobs = []
    for i, sc in enumerate(SCENARIOS):
        if scenario_names and sc.name not in scenario_names:
            continue
        jobs += [(sc.name, s, truth[sc.name]) for s in seeds(set_name, i, n)]
    with _pool() as ex:
        return list(ex.map(_rep_job, jobs, chunksize=4))


def _cp(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    ci = binomtest(k, n).proportion_ci(confidence_level=0.95, method="exact")
    return [float(ci.low), float(ci.high)]


def _rate(vals: list) -> dict:
    v = [x for x in vals if x is not None]
    k = int(sum(bool(x) for x in v))
    return {"k": k, "n": len(v), "rate": (k / len(v)) if v else None, "cp95": _cp(k, len(v))}


def _num(vals: list) -> dict:
    v = np.array([x for x in vals if x is not None], dtype=float)
    if v.size == 0:
        return {"n": 0}
    return {
        "n": int(v.size),
        "mean": float(v.mean()),
        "median": float(np.median(v)),
        "q25": float(np.quantile(v, 0.25)),
        "q75": float(np.quantile(v, 0.75)),
    }


def summarize(results: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r["scenario"], []).append(r)
    out = {}
    for name, rs in by.items():
        s: dict = {"n_replicates": len(rs)}
        d = [r["discovery"] for r in rs]
        s["discovery"] = {
            "false_peak": _rate([x["false_peak"] for x in d]),
            "fundamental_correct": _rate([x["fundamental_correct"] for x in d]),
            "source_flagged": _rate([bool(x["source_flagged_hz"]) for x in d]),
            "status_counts": {k: sum(x["status"] == k for x in d) for k in sorted({x["status"] for x in d})},
        }
        for m in ("standard", "sdof"):
            b = [r[m] for r in rs]
            est = [x for x in b if x["ci_status"] in ("ci_estimated", "ci_limited")]
            s[m] = {
                "ci_status_counts": {
                    k: sum(x["ci_status"] == k for x in b) for k in sorted({x["ci_status"] for x in b})
                },
                "no_result_rate": _rate([x["ci_status"] in ("ci_not_estimable", "no_selection") for x in b]),
                "coverage_policy_mean": _rate([x.get("covers_policy_truth") for x in est]),
                "coverage_physical_amplitude": _rate([x.get("covers_physical_amplitude") for x in est]),
                "ci_width_ln": _num([x.get("ci_width_ln_at_eval") for x in est]),
                "fp_ln_error": _num([x.get("fp_ln_error") for x in b]),
                "fp_abs_ln_error": _num(
                    [None if x.get("fp_ln_error") is None else abs(x["fp_ln_error"]) for x in b]
                ),
                "fp_abs_rel_error": _num(
                    [None if x.get("fp_ln_error") is None else abs(math.exp(x["fp_ln_error"]) - 1) for x in b]
                ),
                "no_fp_rate": _rate([x.get("fp_hz") is None for x in b]),
                "peak_interval_covers_f0": _rate([x.get("peak_interval_covers_f0") for x in b]),
                "peak_multimodal": _rate([x.get("peak_multimodal") for x in b]),
                "windows": _num([x.get("n_windows") for x in b]),
                "informative_blocks": _num([x.get("informative_blocks") for x in b]),
                "uncertainty_s": _num([x.get("uncertainty_s") for x in b]),
            }
        s["sdof"]["mean_r2_selected"] = _num([r["sdof"].get("mean_r2_selected") for r in rs])
        s["sdof"]["mean_r2_standard_windows"] = _num([r["sdof"].get("mean_r2_standard_windows") for r in rs])
        rv = [r["r2_vs_fundamental"] for r in rs]
        s["r2_vs_fundamental"] = {
            "r2_preferred_family_wrong": _rate([x["r2_preferred_family_wrong"] for x in rv]),
            "sdof_r2_up_fp_worse": _rate([x["sdof_r2_up_fp_worse"] for x in rv]),
            "inferred_fundamental_wrong": _rate([x["inferred_fundamental_wrong"] for x in rv]),
        }
        s["compute_s"] = {k: _num([r["times"][k] for r in rs]) for k in ("core_s", "sdof_match_s")}
        out[name] = s
    return out


def evaluate_targets(summary: dict, tlist: list[dict]) -> list[dict]:
    res = []
    for t in tlist:
        s = summary.get(t["scenario"])
        row = dict(t)
        if s is None:
            row.update(result="NOT_EVALUATED")
        elif t["kind"] == "coverage":
            c = s[t["method"]]["coverage_policy_mean"]
            if c["n"] < MIN_EVALUABLE:
                row.update(result="NOT_EVALUABLE", k=c["k"], n=c["n"])
            else:
                p = binomtest(c["k"], c["n"], COVERAGE_TARGET, alternative="less").pvalue
                row.update(
                    k=c["k"],
                    n=c["n"],
                    rate=c["rate"],
                    cp95=c["cp95"],
                    p_value=float(p),
                    result="PASS" if p >= t["alpha"] else "FAIL",
                )
        else:
            c = s["discovery"]["false_peak"]
            p = binomtest(c["k"], c["n"], NULL_FALSE_PEAK_MAX, alternative="greater").pvalue
            row.update(
                k=c["k"],
                n=c["n"],
                rate=c["rate"],
                cp95=c["cp95"],
                p_value=float(p),
                result="PASS" if p >= t["alpha"] else "FAIL",
            )
        res.append(row)
    return res


def results_document(
    set_name: str,
    results: list[dict],
    truth: dict,
    protocol_hash: str | None,
    lock_commit: str | None,
    root: Path,
) -> dict:
    summary = summarize(results)
    tres = evaluate_targets(summary, targets())
    met = all(t["result"] == "PASS" for t in tres)
    truth_meta = {
        k: {m: (None if v[m] is None else {"records": v[m]["records"]}) for m in ("standard", "sdof")}
        for k, v in truth.items()
    }
    return {
        "protocol_id": PROTOCOL_ID,
        "set": set_name,
        "protocol_sha256": protocol_hash,
        "lock_commit": lock_commit,
        "head_commit": git("rev-parse", "HEAD", cwd=root),
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "validated_config_hash": sha256_bytes(canonical_dumps(build_protocol()["analysis_config"]).encode()),
        "replicates_per_scenario": {k: v["n_replicates"] for k, v in summary.items()},
        "truth": truth_meta,
        "summary": summary,
        "targets": tres,
        "verdict": "met" if met else "not_met",
        "replicates": results,
    }


def fingerprint_digest() -> str:
    return hashlib.sha256(canonical_dumps(code_fingerprint()).encode()).hexdigest()
