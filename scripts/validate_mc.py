"""Offline Monte Carlo validation (protocol hvsr-mc-validation-v1).

python scripts/validate_mc.py protocol          # write docs/validation/protocol-v1.json
python scripts/validate_mc.py lock              # hash-lock the COMMITTED protocol
python scripts/validate_mc.py run --set dev     # development set (any time)
python scripts/validate_mc.py run --set test    # locked test set (requires committed protocol + lock)
python scripts/validate_mc.py report            # regenerate docs/validation/report-v1.md
"""

from __future__ import annotations

import argparse
import gzip
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/hvsr_core/src"))

from hvsr_core.jsonutil import pretty_dumps, sha256_bytes, strict_loads  # noqa: E402
from hvsr_core.research import validation_protocol as vp  # noqa: E402

VDIR = ROOT / "docs/validation"
ADIR = ROOT / "artifacts/validation"
PROTOCOL = VDIR / "protocol-v1.json"
LOCK = VDIR / "protocol-v1.lock.json"
REPORT = VDIR / "report-v1.md"


def cmd_protocol(_args) -> None:
    VDIR.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        sys.exit("protocol is locked; create a new protocol version instead of editing v1")
    PROTOCOL.write_bytes(vp.protocol_bytes(vp.build_protocol()))
    print(f"wrote {PROTOCOL.relative_to(ROOT)}; commit it, then run `lock`")


def cmd_lock(_args) -> None:
    if LOCK.exists():
        sys.exit("already locked")
    data = PROTOCOL.read_bytes()
    if data != vp.protocol_bytes(vp.build_protocol()):
        sys.exit("protocol file is stale relative to the code; regenerate before locking")
    rel = str(PROTOCOL.relative_to(ROOT))
    vp.git("ls-files", "--error-unmatch", rel, cwd=ROOT)
    if vp.git("status", "--porcelain", "--", rel, cwd=ROOT):
        sys.exit("commit the protocol before locking")
    lock = {
        "protocol_id": vp.PROTOCOL_ID,
        "protocol_file": rel,
        "protocol_sha256": sha256_bytes(data),
        "protocol_commit": vp.git("log", "-n", "1", "--format=%H", "--", rel, cwd=ROOT),
        "locked_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "statement": "Counts, seeds, metrics and acceptance rules were fixed before the locked test set was run.",
    }
    LOCK.write_text(pretty_dumps(lock))
    print(f"wrote {LOCK.relative_to(ROOT)}; commit it before `run --set test`")


def cmd_run(args) -> None:
    ADIR.mkdir(parents=True, exist_ok=True)
    phash = lock_commit = None
    if args.set == "test":
        info = vp.check_lock(ROOT, PROTOCOL, LOCK)
        phash, lock_commit = info["protocol_sha256"], info["lock_commit"]
        n = vp.REPLICATES["test"]
        names = None
    else:
        n = args.n or vp.REPLICATES["dev"]
        names = args.scenarios
    t0 = time.perf_counter()
    truth = vp.compute_truth(
        vp.REPLICATES["truth"] if args.set == "test" else (args.n_truth or vp.REPLICATES["truth"]), names
    )
    t1 = time.perf_counter()
    results = vp.run_set(args.set, n, truth, names)
    t2 = time.perf_counter()
    doc = vp.results_document(args.set, results, truth, phash, lock_commit, ROOT)
    doc["wall_time_s"] = {"truth": t1 - t0, "replicates": t2 - t1}
    reps = doc.pop("replicates")
    (ADIR / f"{args.set}-v1-summary.json").write_text(pretty_dumps(doc))
    with gzip.open(ADIR / f"{args.set}-v1-replicates.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(pretty_dumps(reps))
    print(f"{args.set}: {len(reps)} replicates in {t2 - t0:.0f} s; verdict {doc['verdict']}")
    for t in doc["targets"]:
        print(f"  {t['result']:<14} {t['id']}: {t.get('k')}/{t.get('n')}  p={t.get('p_value')}")
    if args.set == "test":
        write_report(doc)


def _pct(r: dict | None) -> str:
    if not r or r.get("rate") is None:
        return "n/a"
    lo, hi = r["cp95"]
    return f"{100 * r['rate']:.1f}% ({r['k']}/{r['n']}; {100 * lo:.1f}-{100 * hi:.1f})"


def _med(x: dict, key: str = "median", fmt: str = "{:.3f}") -> str:
    return fmt.format(x[key]) if x.get("n") else "n/a"


def write_report(doc: dict) -> None:
    lines = [
        "# Monte Carlo validation report (protocol hvsr-mc-validation-v1, locked test set)",
        "",
        f"- Protocol SHA-256: `{doc['protocol_sha256']}` (lock commit `{doc['lock_commit']}`)",
        f"- Code commit at run time: `{doc['head_commit']}`; generated {doc['generated_utc']}",
        f"- Replicates per scenario: {sorted(set(doc['replicates_per_scenario'].values()))}; truth records per scenario: {vp.REPLICATES['truth']}",
        f"- **AC-S5-06 verdict: {'MET' if doc['verdict'] == 'met' else 'NOT MET'}**",
        "",
        "The CI targets the selection-policy mean curve (Standard: all quality-screened non-overlapping windows; SDOF:",
        "R2-selected windows in the family matching the true fundamental). Coverage of the physical transfer-function",
        "amplitude and of the true resonance frequency is reported separately and is not a target. Coverage is computed",
        "over replicates whose CI was estimated or limited; the no-result rate shows how often no CI was produced.",
        "",
        "## Predeclared targets",
        "",
        "| Target | Result | k/n | rate | exact 95% CI | p (one-sided) | alpha |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in doc["targets"]:
        cp = t.get("cp95")
        lines.append(
            f"| {t['id']} | {t['result']} | {t.get('k')}/{t.get('n')} | "
            f"{'n/a' if t.get('rate') is None else f'{100 * t["rate"]:.1f}%'} | "
            f"{'n/a' if not cp else f'{100 * cp[0]:.1f}-{100 * cp[1]:.1f}%'} | "
            f"{'n/a' if t.get('p_value') is None else f'{t["p_value"]:.2g}'} | {t['alpha']:.2g} |"
        )
    lines += ["", "## Per-scenario results", ""]
    for name, s in doc["summary"].items():
        d = s["discovery"]
        lines += [
            f"### {name}",
            "",
            f"Discovery: fundamental correct {_pct(d['fundamental_correct'])}; false plausible peak {_pct(d['false_peak'])}; "
            f"source flag {_pct(d['source_flagged'])}.",
            "",
            "| Metric | Standard | SDOF |",
            "|---|---|---|",
        ]
        st, sd = s["standard"], s["sdof"]
        rows = [
            (
                "CI coverage of policy mean",
                _pct(st["coverage_policy_mean"]),
                _pct(sd["coverage_policy_mean"]),
            ),
            (
                "CI coverage of physical amplitude",
                _pct(st["coverage_physical_amplitude"]),
                _pct(sd["coverage_physical_amplitude"]),
            ),
            ("median CI width (ln)", _med(st["ci_width_ln"]), _med(sd["ci_width_ln"])),
            ("no-result rate", _pct(st["no_result_rate"]), _pct(sd["no_result_rate"])),
            ("CI status counts", str(st["ci_status_counts"]), str(sd["ci_status_counts"])),
            (
                "fp bias, mean ln(fp/f0)",
                _med(st["fp_ln_error"], "mean", "{:+.4f}"),
                _med(sd["fp_ln_error"], "mean", "{:+.4f}"),
            ),
            (
                "median abs ln error",
                _med(st["fp_abs_ln_error"], "median", "{:.4f}"),
                _med(sd["fp_abs_ln_error"], "median", "{:.4f}"),
            ),
            (
                "mean abs relative fp error",
                _med(st["fp_abs_rel_error"], "mean", "{:.4f}"),
                _med(sd["fp_abs_rel_error"], "mean", "{:.4f}"),
            ),
            ("no fp in family", _pct(st["no_fp_rate"]), _pct(sd["no_fp_rate"])),
            (
                "peak interval covers f0 (conditional)",
                _pct(st["peak_interval_covers_f0"]),
                _pct(sd["peak_interval_covers_f0"]),
            ),
            (
                "median windows retained",
                _med(st["windows"], "median", "{:.0f}"),
                _med(sd["windows"], "median", "{:.0f}"),
            ),
            (
                "median informative blocks",
                _med(st["informative_blocks"], "median", "{:.0f}"),
                _med(sd["informative_blocks"], "median", "{:.0f}"),
            ),
            ("median uncertainty time (s)", _med(st["uncertainty_s"]), _med(sd["uncertainty_s"])),
        ]
        lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
        rv = s["r2_vs_fundamental"]
        lines += [
            "",
            f"R2 vs fundamental: R2-preferred family wrong {_pct(rv['r2_preferred_family_wrong'])}; "
            f"SDOF mean R2 above Standard windows while |fp error| worse {_pct(rv['sdof_r2_up_fp_worse'])}; "
            f"inferred fundamental wrong {_pct(rv['inferred_fundamental_wrong'])}. "
            f"Median compute: core {_med(s['compute_s']['core_s'])} s, SDOF matching {_med(s['compute_s']['sdof_match_s'])} s.",
            "",
        ]
    lines += [
        "## Scope",
        "",
        "- All signals are synthetic (`synthetic-3c-v1`): independent Gaussian components shaped by a known transfer",
        "  function. They are research fixtures, not physical wavefield simulations; real-data coverage is not established.",
        "- The layered case is a 1D SH transfer function computed by this project, not a published benchmark (AC-S5-07 BLOCKED).",
        "- The 95 % label remains nominal: `coverage_calibrated` stays false in every analysis output.",
    ]
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"wrote {REPORT.relative_to(ROOT)}")


def cmd_report(_args) -> None:
    write_report(strict_loads((ADIR / "test-v1-summary.json").read_text()))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("protocol").set_defaults(fn=cmd_protocol)
    sub.add_parser("lock").set_defaults(fn=cmd_lock)
    r = sub.add_parser("run")
    r.add_argument("--set", choices=["dev", "test"], required=True)
    r.add_argument("--n", type=int, default=None, help="dev only: replicates per scenario")
    r.add_argument("--n-truth", type=int, default=None, help="dev only: truth records per scenario")
    r.add_argument("--scenarios", nargs="*", default=None, help="dev only: subset of scenarios")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
