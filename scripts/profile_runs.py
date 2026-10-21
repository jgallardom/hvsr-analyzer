"""Profile submission, cold analysis (runs A and B), and warm-cache reporting on this machine.

    uv run python scripts/profile_runs.py                       # all cases -> docs/evidence/stage9/
    uv run python scripts/profile_runs.py --cases small --out /tmp/perf.json --no-markdown

Operational limits come from HVSR_* environment variables (defaults otherwise), so a hosted budget can
be checked directly; --blueprint applies the web service's literal limits from a Render blueprint:

    uv run python scripts/profile_runs.py --blueprint render.yaml --cases large eight_hours \
        --out docs/evidence/stage9/performance-render-limits.json --no-markdown

Each case is a deterministic synthetic recording (seeded) submitted as a gzip CSV to a throwaway
local site. Every phase runs in a fresh process, so peak memory is per phase (it includes the
interpreter and library baseline, reported separately). Analyses run through the production worker
path (`isolation.run_isolated_once`, numerical threads pinned). Counts (candidate windows, template
comparisons, bootstrap replicates, artifact bytes except figures) are deterministic; figure bytes vary
slightly because each footer prints the random run identifier. Timings depend on the declared
hardware and load.
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import platform
import resource
import secrets
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "small": {"duration_s": 900, "fs": 100.0, "seed": 101},
    "medium": {"duration_s": 3600, "fs": 100.0, "seed": 102},
    "large": {"duration_s": 7200, "fs": 200.0, "seed": 103},
    # the same recording as `large` written with twice the decimals: upload memory against text size
    "large_wide": {"duration_s": 7200, "fs": 200.0, "seed": 103, "decimals": 12},
    # the most candidate windows the Render blueprint's sample and window caps admit
    "eight_hours": {"duration_s": 28800, "fs": 50.0, "seed": 106},
    "long": {"duration_s": 86400, "fs": 50.0, "seed": 104},
}
SDOF_FN_HZ = 2.0


def synthetic_csv_gz(duration_s: float, fs: float, seed: int, decimals: int = 6) -> bytes:
    from hvsr_core.research.synthetic import TFSpec, generate_3c

    rec, _ = generate_3c(
        duration_s=duration_s, fs=fs, tf=TFSpec("sdof", {"fn": SDOF_FN_HZ, "zeta": 0.2}), seed=seed
    )
    data = rec.segments[0].data
    buf = io.StringIO()
    buf.write("time_s,N,E,Z\n")
    d = decimals
    for i in range(data.shape[1]):
        buf.write(f"{i / fs:.3f},{data[0, i]:.{d}f},{data[1, i]:.{d}f},{data[2, i]:.{d}f}\n")
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(buf.getvalue().encode())
    return out.getvalue()


def _settings(site: str):
    """Operational limits from HVSR_* environment variables (defaults otherwise) on a throwaway site."""
    from hvsr_web.settings import Settings

    return Settings.from_env(
        {
            **os.environ,
            "HVSR_DATABASE_URL": f"sqlite:///{site}/db.sqlite3",
            "HVSR_OBJECT_STORE": "local",
            "HVSR_LOCAL_STORE_DIR": f"{site}/objects",
            "HVSR_BANK_DIR": f"{site}/banks",
            "HVSR_URL_SIGNING_KEY": secrets.token_hex(32),
            "HVSR_JOB_ISOLATION": "process",
        }
    )


def _db_bytes(site: str) -> int:
    return sum(p.stat().st_size for p in Path(site).glob("db.sqlite3*"))


def _peak_rss() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def phase_baseline() -> dict:
    import hvsr_web.api  # noqa: F401  - the import baseline of every worker process

    return {"peak_rss_bytes": _peak_rss()}


def phase_generate(site: str, case: dict) -> dict:
    data = synthetic_csv_gz(case["duration_s"], case["fs"], case["seed"], case.get("decimals", 6))
    Path(site, "upload.csv.gz").write_bytes(data)
    return {"upload_bytes": len(data), "decoded_bytes": len(gzip.decompress(data))}


def phase_submit(site: str, case: dict) -> dict:
    from hvsr_core.metadata import MeasurementMetadata
    from hvsr_web.auth import issue_upload_token
    from hvsr_web.context import AppContext
    from hvsr_web.isolation import pin_threads
    from hvsr_web.licensing import ConsentForm
    from hvsr_web.submissions import SubmissionRequest, UploadFile, WaveformOptions, submit

    settings = _settings(site)
    pin_threads(settings.numeric_threads)
    data = Path(site, "upload.csv.gz").read_bytes()
    ctx = AppContext.from_settings(settings)
    with ctx.sessions() as session, session.begin():
        _, c = issue_upload_token(session, private_email="profiler@example.org")
        cid = c.id
    db_before = _db_bytes(site)
    with ctx.sessions() as session:
        from hvsr_web.db import Contributor

        contributor = session.get(Contributor, cid)
    req = SubmissionRequest(
        idempotency_key=f"profile-{case['seed']}",
        metadata=MeasurementMetadata(latitude=40.4168, longitude=-3.7038),
        consent=ConsentForm(consent_checked=True, rights_confirmed=True, attribution="Profiler"),
        waveform=WaveformOptions(csv_layout="elapsed_time"),
    )
    t0, c0 = time.perf_counter(), time.process_time()
    r = submit(ctx, contributor, req, [UploadFile("profile.csv.gz", data)])
    return {
        "measurement_id": r.measurement_id,
        "contributor_id": cid,
        "database_bytes_before": db_before,
        "upload_bytes": len(data),
        "logical_bytes": r.charged_logical_bytes,
        "stored_bytes": r.stored_bytes,
        "wall_s": time.perf_counter() - t0,
        "cpu_s": time.process_time() - c0,
        "peak_rss_bytes": _peak_rss(),
    }


def phase_analysis(site: str, measurement_id: str, contributor_id: str, request: dict) -> dict:
    from hvsr_core.config import AnalysisRequest
    from hvsr_web import analysis_jobs, isolation
    from hvsr_web.context import AppContext

    ctx = AppContext.from_settings(_settings(site), run_migrations=False)
    out = analysis_jobs.request_analysis(
        ctx, measurement_id, AnalysisRequest.model_validate(request), contributor_id
    )
    t0 = time.perf_counter()
    res = isolation.run_isolated_once(ctx, "profiler")
    wall = time.perf_counter() - t0
    if res is None or res.get("state") != "succeeded":
        raise RuntimeError(f"analysis job failed: {res}")
    return {"estimate": out["estimate"], "job_wall_s": wall, **res}


def run_counts(site: str, run_ids: list[str]) -> dict:
    from sqlalchemy import select

    from hvsr_core.jsonutil import strict_loads
    from hvsr_web.context import AppContext
    from hvsr_web.db import Artifact

    ctx = AppContext.from_settings(_settings(site), run_migrations=False)
    out = {}
    with ctx.sessions() as session:
        for run_id in run_ids:
            arts = session.scalars(select(Artifact).where(Artifact.run_id == run_id)).all()
            docs = {
                a.method: strict_loads(ctx.store.get(a.blob_key))
                for a in arts
                if a.kind == "analysis_json" and a.generated_by["report_options"]["evaluate_sesame"]
            }
            std, sdof = docs["standard"], docs["sdof"]
            matched = 0
            for sel in sdof["selections"]:
                ec = sel["templates"]["eligibility_counts"]
                matched += ec["eligible"] + ec["no_meaningful_match"] + ec["undefined_r2"]
            from hvsr_core.templates import get_bank

            n_templates = get_bank(sdof["provenance"]["template_bank"]["bank_id"]).n_templates
            # One replicate set per block length that could be estimated (base plus sensitivity lengths).
            boots = [
                (s["uncertainty"].get("n_bootstrap") or 0)
                * sum(1 for x in s["uncertainty"].get("sensitivity") or [] if x["status"] == "estimated")
                for d in docs.values()
                for s in d["selections"]
                if s["uncertainty"].get("status") in ("ci_estimated", "ci_limited")
            ]
            ci_status = {m: [s["uncertainty"]["status"] for s in d["selections"]] for m, d in docs.items()}
            stored = [a for a in arts if a.blob_key]
            out[run_id] = {
                "window_length_s": std["recording"]["window_length_s"],
                "candidate_windows": std["windows"]["candidate_count"],
                "qc_passed_windows": std["windows"]["qc_passed_count"],
                "selected_windows": {m: [s["n_selected"] for s in d["selections"]] for m, d in docs.items()},
                "sdof_windows_matched": matched,
                "templates_in_bank": n_templates,
                "template_comparisons": matched * n_templates,
                "bootstrap_replicates": sum(boots),
                "bootstrapped_selections": len(boots),
                "ci_status": ci_status,
                "artifacts_stored": len(stored),
                "artifact_bytes": sum(a.byte_size or 0 for a in stored),
                "artifact_bytes_by_category": {
                    cat: sum(a.byte_size or 0 for a in stored if a.category == cat)
                    for cat in sorted({a.category for a in stored})
                },
                "on_request_artifacts": sorted(a.kind for a in arts if not a.blob_key),
            }
    return out


def _in_child(fn, *args):
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        return pool.submit(fn, *args).result()


def hardware() -> dict:
    from threadpoolctl import threadpool_info

    cpu = next(
        (
            ln.split(":", 1)[1].strip()
            for ln in Path("/proc/cpuinfo").read_text().splitlines()
            if ln.startswith("model name")
        ),
        platform.processor() or "unknown",
    )
    mem = next(
        (
            int(ln.split()[1]) * 1024
            for ln in Path("/proc/meminfo").read_text().splitlines()
            if ln.startswith("MemTotal")
        ),
        None,
    )
    import numpy

    return {
        "cpu_model": cpu,
        "logical_cpus": os.cpu_count(),
        "memory_bytes": mem,
        "os": platform.platform(),
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "blas": sorted({f"{p['internal_api']} {p.get('version')}" for p in threadpool_info()}),
        "numeric_threads_per_process": 1,
        "storage": "local filesystem object store and SQLite in a temporary directory",
    }


def profile_case(name: str, case: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"hvsr-profile-{name}-") as site:
        gen = _in_child(phase_generate, site, case)
        sub = _in_child(phase_submit, site, case) | {"decoded_bytes": gen["decoded_bytes"]}
        cold = _in_child(phase_analysis, site, sub["measurement_id"], sub["contributor_id"], {})
        warm = _in_child(
            phase_analysis,
            site,
            sub["measurement_id"],
            sub["contributor_id"],
            {"report": {"evaluate_sesame": False}},
        )
        run_ids = [cold["result"]["run_id"]] + (
            [cold["result"]["run_b_id"]] if cold["result"]["run_b_id"] else []
        )
        counts = _in_child(run_counts, site, run_ids)
        db_after = _db_bytes(site)
    samples = int(case["duration_s"] * case["fs"])

    def phase(r: dict) -> dict:
        return {
            "job_wall_s": round(r["job_wall_s"], 3),
            "handler_wall_s": round(r["result"]["wall_s"], 3),
            "handler_cpu_s": round(r["result"]["cpu_s"], 3),
            "process_cpu_s": r["process"]["cpu_s"],
            "peak_rss_bytes": r["process"]["peak_rss_bytes"],
            "science_cached": r["result"]["science_cached"],
        }

    return {
        "case": name,
        "recording": {
            **case,
            "samples_per_channel": samples,
            "kind": f"synthetic SDOF response, fn {SDOF_FN_HZ} Hz",
        },
        "submission": {
            k: (round(v, 3) if isinstance(v, float) else v)
            for k, v in sub.items()
            if k not in ("measurement_id", "contributor_id", "database_bytes_before")
        },  # fmt: skip
        "budget_estimate": cold["estimate"],
        "database_growth_bytes": db_after - sub["database_bytes_before"],
        "cold": phase(cold),
        "warm_report_only": phase(warm),
        "runs": {("A" if i == 0 else "B"): counts[rid] for i, rid in enumerate(run_ids)},
    }


def _mb(b: int | None) -> str:
    return "n/a" if b is None else f"{b / 1024 / 1024:.0f} MB"


def markdown(doc: dict) -> str:
    hw = doc["hardware"]
    lines = [
        "# Performance report",
        "",
        f"Generated {doc['generated_utc']} by `scripts/profile_runs.py` (software {doc['software_version']}, "
        f"commit `{doc['git_commit']}`{' with uncommitted changes' if doc['git_dirty'] else ''}). "
        "Regenerate with `uv run python scripts/profile_runs.py`.",
        "",
        "## Declared hardware",
        "",
        f"- CPU: {hw['cpu_model']} ({hw['logical_cpus']} logical CPUs); memory {_mb(hw['memory_bytes'])}",
        f"- OS: {hw['os']}; Python {hw['python']}; NumPy {hw['numpy']}; BLAS {', '.join(hw['blas'])}",
        f"- One numerical thread per process; {hw['storage']}.",
        "- These are measurements on this development machine, not a hosted service. Hosted CPUs (for",
        "  example Render instances) differ; rerun this script there before relying on the numbers.",
        "",
        "## Cases",
        "",
        "Deterministic synthetic recordings (seeded), submitted as gzip CSV through the real submission",
        "path. Default analysis configuration: run A (50 s windows) and robustness run B (75 s windows),",
        "SDOF matching, block bootstrap. The warm request changes only a report option, so it must reuse",
        "the stored science. `large_wide` is the `large` recording written with 12 instead of 6 decimals,",
        "to measure how upload memory follows the CSV text size.",
        "",
        "| Case | Duration | Rate | Samples/channel | Upload gzip / CSV text | Candidates A / B | Template comparisons A / B | Bootstrap replicates A / B |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in doc["cases"]:
        r, a, b = c["recording"], c["runs"]["A"], c["runs"].get("B", {})
        lines.append(
            f"| {c['case']} | {r['duration_s'] / 60:.0f} min | {r['fs']:.0f} Hz | {r['samples_per_channel']:,} | "
            f"{_mb(c['submission']['upload_bytes'])} / {_mb(c['submission']['decoded_bytes'])} | "
            f"{a['candidate_windows']} / {b.get('candidate_windows', 'n/a')} | "
            f"{a['template_comparisons']:,} / {b.get('template_comparisons', 0):,} | "
            f"{a['bootstrap_replicates']:,} / {b.get('bootstrap_replicates', 0):,} |"
        )
    lines += [
        "",
        "Bootstrap replicates count every replicate set drawn (base block length plus each predeclared",
        "sensitivity length that has enough informative blocks). Zero means no curve CI was estimable;",
        "the CI status of each published selection is:",
        "",
    ]
    for c in doc["cases"]:
        for run_id, run in c["runs"].items():
            st = "; ".join(f"{m}: {', '.join(v) or 'no selection'}" for m, v in run["ci_status"].items())
            lines.append(f"- {c['case']} run {run_id}: {st}")
    lines += [
        "",
        "## Time and memory",
        "",
        "Peak memory is the resident-set peak of the process doing the phase, including the import",
        f"baseline of {_mb(doc['import_baseline_rss_bytes'])}. Job wall time includes starting the isolated",
        "job process.",
        "",
        "| Case | Submit wall / CPU | Submit peak | Cold job wall | Cold CPU | Cold peak | Warm job wall | Warm CPU | Warm peak | Warm reused science |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in doc["cases"]:
        s, co, w = c["submission"], c["cold"], c["warm_report_only"]
        lines.append(
            f"| {c['case']} | {s['wall_s']:.1f} s / {s['cpu_s']:.1f} s | {_mb(s['peak_rss_bytes'])} | "
            f"{co['job_wall_s']:.1f} s | {co['process_cpu_s']:.1f} s | {_mb(co['peak_rss_bytes'])} | "
            f"{w['job_wall_s']:.1f} s | {w['process_cpu_s']:.1f} s | {_mb(w['peak_rss_bytes'])} | "
            f"{'yes' if w['science_cached'] else 'NO'} |"
        )
    lines += [
        "",
        "## Stored bytes",
        "",
        "| Case | Original stored | Run A artifacts | Run B artifacts | Per-window spectra | Database growth |",
        "|---|---|---|---|---|---|",
    ]
    for c in doc["cases"]:
        a, b = c["runs"]["A"], c["runs"].get("B", {})
        lines.append(
            f"| {c['case']} | {_mb(c['submission']['stored_bytes'])} | {a['artifacts_stored']} files, "
            f"{_mb(a['artifact_bytes'])} | {b.get('artifacts_stored', 0)} files, {_mb(b.get('artifact_bytes', 0))} | "
            f"{'on request' if 'window_spectra' in a['on_request_artifacts'] else 'stored or unavailable'} | "
            f"{c['database_growth_bytes'] / 1024:.0f} KB |"
        )
    lines += [
        "",
        "Database growth is the SQLite file growth for one measurement and its analysis (rows, JSON",
        "summaries, ledger entries); object bytes live in the object store.",
    ]
    worst = max(doc["cases"], key=lambda c: c["cold"]["process_cpu_s"])
    sub_worst = max(doc["cases"], key=lambda c: c["submission"]["peak_rss_bytes"])
    most = max(doc["cases"], key=lambda c: c["runs"]["A"]["candidate_windows"])
    lim = doc["limits"]
    cap_h = (
        most["recording"]["duration_s"]
        * lim["max_candidate_windows"]
        / most["runs"]["A"]["candidate_windows"]
        / 3600
    )
    lines += [
        "",
        "## Budgets against measurements",
        "",
        f"- Largest measured cold job: {worst['case']}, {worst['cold']['process_cpu_s']:.1f} s CPU, "
        f"{worst['cold']['job_wall_s']:.1f} s wall, {_mb(worst['cold']['peak_rss_bytes'])} peak.",
        f"- `HVSR_JOB_CPU_TIME_S` = {lim['job_cpu_time_s']} s and `HVSR_JOB_WALL_TIME_S` = {lim['job_wall_time_s']} s: "
        f"{lim['job_cpu_time_s'] / max(worst['cold']['process_cpu_s'], 1e-9):.0f}x the largest measured CPU time here,",
        "  which leaves room for slower hosted CPUs. A job that exceeds either limit is stopped and recorded",
        "  as a permanent failure (no retry).",
        f"- `HVSR_JOB_MEMORY_BYTES` = {_mb(lim['job_memory_bytes'])} above the job-process baseline "
        f"({_mb(doc['import_baseline_rss_bytes'])}); the largest measured job peak was",
        f"  {_mb(worst['cold']['peak_rss_bytes'])} including the baseline. A worker instance needs at least that much "
        "memory per concurrent job.",
        f"- `HVSR_MAX_CANDIDATE_WINDOWS` = {lim['max_candidate_windows']:,}: the {most['case']} case "
        f"({most['recording']['duration_s'] / 3600:g} h) produced {most['runs']['A']['candidate_windows']:,} candidate "
        f"windows with the default 50 s windows,",
        f"  so the cap admits roughly {cap_h:.0f} h with default windows; longer requests are rejected by the cost",
        "  estimate before any computation, even though `HVSR_MAX_DURATION_S` is larger.",
        f"- Uploads are read and parsed in the web process, at most `HVSR_MAX_CONCURRENT_UPLOADS` = "
        f"{lim['max_concurrent_uploads']} at a time",
        f"  (others wait up to `HVSR_UPLOAD_WAIT_S` = {lim['upload_wait_s']:g} s, then receive 503 busy). The largest "
        f"submission peak was {_mb(sub_worst['submission']['peak_rss_bytes'])}",
        f"  ({sub_worst['case']}, {sub_worst['recording']['samples_per_channel']:,} samples per channel as CSV). Size "
        "the web instance for that many",
        "  of the largest uploads you accept, or lower `HVSR_MAX_REQUEST_BYTES` and `HVSR_MAX_SAMPLES_PER_CHANNEL`.",
        f"- `HVSR_MAX_TEMPLATE_COMPARISONS` = {lim['max_template_comparisons']:,} per family: the shipped SDOF banks hold "
        f"{most['runs']['A']['templates_in_bank']} templates, so comparisons stay far below the cap; it matters for",
        "  large uploaded banks.",
        "- No speed claim is made beyond these measurements.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", nargs="*", default=list(CASES), choices=list(CASES))
    ap.add_argument("--out", type=Path, default=ROOT / "docs/evidence/stage9/performance.json")
    ap.add_argument("--markdown", type=Path, default=ROOT / "docs/performance.md")
    ap.add_argument("--no-markdown", action="store_true")
    ap.add_argument("--blueprint", type=Path, help="apply the web service's HVSR_* limits from render.yaml")
    args = ap.parse_args()
    if args.blueprint:
        import yaml

        [web] = [s for s in yaml.safe_load(args.blueprint.read_text())["services"] if s["type"] == "web"]
        limits = {e["key"]: str(e["value"]) for e in web["envVars"] if "value" in e}
        os.environ.update(
            {k: v for k, v in limits.items() if k.startswith("HVSR_") and k != "HVSR_OBJECT_STORE"}
        )

    from hvsr_core.analysis import _git
    from hvsr_core.jsonutil import pretty_dumps
    from hvsr_core.versions import PACKAGE_VERSION

    git = _git()  # the checkout state the analyses record in their provenance
    cases = []
    for name in args.cases:
        t0 = time.perf_counter()
        cases.append(profile_case(name, CASES[name]))
        print(f"{name}: {time.perf_counter() - t0:.1f} s", file=sys.stderr)
    doc = {
        "report": "hvsr-analyzer performance profile",
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "software_version": PACKAGE_VERSION,
        "git_commit": git.get("git_commit"),
        "git_dirty": git.get("git_dirty"),
        "hardware": hardware(),
        "import_baseline_rss_bytes": _in_child(phase_baseline)["peak_rss_bytes"],
        "limits": _settings(tempfile.gettempdir()).public_limits(),
        "cases": cases,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(pretty_dumps(doc))
    if not args.no_markdown:
        args.markdown.write_text(markdown(doc))
    print(f"wrote {args.out}" + ("" if args.no_markdown else f" and {args.markdown}"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
