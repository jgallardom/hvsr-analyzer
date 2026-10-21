"""`hvsr`: offline command-line tool for researchers (spec 14 and 16).

    hvsr analyze REC... --out DIR [--request request.json] [--csv-layout elapsed_time] [--plots]
    hvsr replay ANALYSIS.json... --recording REC... [--csv-layout ...]
    hvsr banks build --family sdof --conditioning ko:40 --bank-id sdof-ko40-lab-v2 --out DIR
    hvsr banks validate DIR/BANK.json [--hmac-key-env VAR] [--require-signature]
    hvsr banks list
    hvsr mc --n 10 --n-truth 20 --out DIR          (development set only; exploratory)
    hvsr docs --out DIR
    hvsr schema NAME

Exit codes: 0 success; 1 replay differs or validation failed; 2 invalid input or usage.
Everything here reads and writes data files only (JSON, CSV, NPZ without pickled objects).
"""

from __future__ import annotations

import argparse
import gzip
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from . import exports, plots, templates
from .analysis import AnalysisCache, RecordInfo, run_analysis
from .config import AnalysisRequest
from .io.csv_reader import CSVOptions, read_csv
from .io.limits import InputLimits
from .io.mseed_reader import read_miniseed
from .io.preservation import detect_format
from .io.stationxml import apply_stationxml, parse_stationxml
from .jsonutil import canonical_dumps, pretty_dumps, sha256_bytes, strict_loads
from .metadata import MeasurementMetadata, apply_sensor_metadata, evaluate_instrument
from .model import InputError
from .schema_validation import SCHEMA_FILES, load_schema

GiB = 1024**3
OFFLINE_LIMITS = InputLimits(
    max_request_bytes=8 * GiB,
    max_decoded_bytes=32 * GiB,
    max_samples_per_channel=1_000_000_000,
    max_channels=64,
    max_files=256,
    max_duration_s=31 * 86_400.0,
)


class UsageError(Exception):
    pass


# ------------------------------------------------------------------------------------- recordings
def _waveform_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--csv-layout", choices=["elapsed_time", "utc_timestamps", "samples_with_rate"])
    p.add_argument("--csv-sampling-rate-hz", type=float)
    p.add_argument("--csv-start-time-utc")
    p.add_argument("--csv-timestamps-declared-utc", action="store_true")
    p.add_argument("--csv-column-map", help='JSON, e.g. {"N": "north", "E": "east", "Z": "up"}')
    p.add_argument("--miniseed-mapping", help='JSON, e.g. {"N": "XX.STA.00.HHN", ...}')
    p.add_argument("--stationxml", type=Path, help="StationXML response metadata")
    p.add_argument("--metadata", type=Path, help="MeasurementMetadata JSON (sensor declarations)")


def _json_arg(text: str | None):
    return None if text is None else strict_loads(text)


def load_recording(paths: list[Path], args) -> tuple:
    """(recording, source SHA-256s, StationXML SHA-256 or None), parsed like the web service."""
    files = [(p.name, p.read_bytes()) for p in paths]
    fmts = [detect_format(n, d) for n, d in files]
    if any(f in ("csv", "csv.gz") for f in fmts):
        if len(files) != 1:
            raise UsageError("a CSV recording must be one file with all three components")
        if not args.csv_layout:
            raise UsageError("--csv-layout is required for CSV recordings")
        opts = CSVOptions(
            layout=args.csv_layout,
            column_map=_json_arg(args.csv_column_map),
            sampling_rate_hz=args.csv_sampling_rate_hz,
            start_time_utc=args.csv_start_time_utc,
            timestamps_declared_utc=args.csv_timestamps_declared_utc,
        )
        rec, _ = read_csv(files[0][1], opts, OFFLINE_LIMITS, filename=files[0][0])
    else:
        rec = read_miniseed(files, mapping=_json_arg(args.miniseed_mapping), limits=OFFLINE_LIMITS)
    response_sha = None
    if args.stationxml:
        data = args.stationxml.read_bytes()
        response_sha = sha256_bytes(data)
        rec, _ = apply_stationxml(rec, parse_stationxml(data), response_sha)
    return rec, [sha256_bytes(d) for _, d in files], response_sha


def _record_info(rec, args, request: AnalysisRequest, sources: list[str], response_sha) -> tuple:
    meta = (
        MeasurementMetadata.model_validate(strict_loads(args.metadata.read_text())) if args.metadata else None
    )
    if meta is not None:
        rec = apply_sensor_metadata(rec, meta.sensor)
    info = RecordInfo(
        latitude=meta.latitude if meta else None,
        longitude=meta.longitude if meta else None,
        measurement_context=meta.measurement_context if meta else "unknown",
        input_hashes={"recording_sources": sources},
        response_sha256=response_sha,
        instrument=evaluate_instrument(
            rec, meta.sensor if meta else None, request.config.preprocessing.response_correction
        ),
    )
    return rec, info


# ----------------------------------------------------------------------------------------- analyze
def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)


def _write_run(out: Path, res, with_plots: bool, display: dict) -> list[str]:
    written = []
    sup = res.supplement
    for method, doc in res.published.items():
        _write(out / f"analysis_{method}.json", canonical_dumps(doc))
        rows = exports.mean_spectrum_rows(doc, sup["grid_invalid_reason"])
        _write(out / f"mean_spectrum_{method}.csv", exports.table_csv("mean_spectrum", rows))
        written += [f"analysis_{method}.json", f"mean_spectrum_{method}.csv"]
        if method in sup["window_tables"]:
            name = exports.window_table_name(method)
            _write(out / f"window_table_{method}.csv", exports.table_csv(name, sup["window_tables"][method]))
            written.append(f"window_table_{method}.csv")
        if with_plots and doc["selections"]:
            recipe = plots.make_recipe(doc, fmt="svg", display=display)
            _write(out / f"plot_{method}.svg", plots.render(doc, recipe))
            written.append(f"plot_{method}.svg")
    return written


def cmd_analyze(args) -> int:
    request = (
        AnalysisRequest.model_validate(strict_loads(args.request.read_text()))
        if args.request
        else AnalysisRequest()
    )
    rec, sources, response_sha = load_recording(args.recording, args)
    rec, info = _record_info(rec, args, request, sources, response_sha)
    cache = AnalysisCache(args.cache) if args.cache else None
    t0 = time.perf_counter()
    res = run_analysis(rec, request, info, cache=cache)
    wall = time.perf_counter() - t0
    out: Path = args.out
    display = request.display.model_dump(mode="json")
    written = ["request.json", *_write_run(out, res, args.plots, display)]
    _write(out / "request.json", pretty_dumps(request.model_dump(mode="json")))
    if res.robustness is not None:
        _write(out / "robustness_comparison.json", pretty_dumps(res.robustness))
        written.append("robustness_comparison.json")
    if res.run_b is not None:
        written += [f"run_b/{f}" for f in _write_run(out / "run_b", res.run_b, args.plots, display)]
    summary = {
        "run_id": res.run_id,
        "science_key": res.key,
        "science_cached": res.cached,
        "wall_s": round(wall, 3),
        "methods": {m: d["method_status"] for m, d in res.published.items()},
        "external": "offline result: not a server-verified publication",
        "written": written,
    }
    print(pretty_dumps(summary))
    return 0


# ------------------------------------------------------------------------------------------ replay
def cmd_replay(args) -> int:
    from .replay import replay_published

    docs = [strict_loads(p.read_text()) for p in args.analysis]
    rec, sources, _ = load_recording(args.recording, args)
    published = docs[0]["provenance"].get("input_hashes", {}).get("recording_sources")
    report = replay_published(rec, docs)
    report["input_files_match_published_sources"] = (
        None if not published else sorted(sources) == sorted(published)
    )
    print(pretty_dumps(report))
    return 0 if report["status"] == "reproduced" else 1


# ------------------------------------------------------------------------------------------- banks
def _floats(text: str | None) -> tuple[float, ...] | None:
    return None if text is None else tuple(float(x) for x in text.split(","))


def _conditioning(text: str) -> dict:
    if text == "raw":
        return templates.RAW
    if text.startswith("ko:"):
        return templates.ko_conditioning(float(text[3:]))
    raise UsageError("--conditioning must be 'raw' or 'ko:<b>' (Konno-Ohmachi bandwidth)")


def cmd_banks_build(args) -> int:
    cond = _conditioning(args.conditioning)
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.family == "sdof":
        kw = {"damping": _floats(args.damping)} if args.damping else {}
        manifest, data = templates.generate_sdof_bank(
            cond, bank_id=args.bank_id, bank_version=args.bank_version, created_utc=created, **kw
        )
    else:
        from .mdof import generate_mdof_bank

        if not (args.mass_ratios and args.stiffness_ratios and args.damping):
            raise UsageError("MDOF banks need --mass-ratios, --stiffness-ratios, and --damping")
        manifest, data = generate_mdof_bank(
            n_dof=int(args.family[-1]),
            conditioning=cond,
            mass_ratio_grid=_floats(args.mass_ratios),
            stiffness_ratio_grid=_floats(args.stiffness_ratios),
            damping_grid=_floats(args.damping),
            bank_id=args.bank_id,
            bank_version=args.bank_version,
            created_utc=created,
        )
    if args.license:
        manifest["license"] = args.license
    if args.sign_key_env:
        key = os.environ.get(args.sign_key_env)
        if not key:
            raise UsageError(f"environment variable {args.sign_key_env} is empty")
        manifest = templates.sign_manifest(manifest, key.encode(), args.key_id or args.sign_key_env)
    js, npz = templates.write_bank(args.out, manifest, data)
    templates.load_bank_files(js)  # the written files pass the same validation as an upload
    print(pretty_dumps({"manifest": str(js), "data": str(npz), "bank_id": manifest["bank_id"],
                        "n_templates": manifest["n_templates"], "data_sha256": manifest["data_sha256"]}))  # fmt: skip
    return 0


def cmd_banks_validate(args) -> int:
    key = None
    if args.hmac_key_env:
        key = (os.environ.get(args.hmac_key_env) or "").encode() or None
    try:
        bank = templates.load_bank_files(
            args.manifest, hmac_key=key, require_signature=args.require_signature
        )
    except (templates.BankError, OSError, ValueError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    m = bank.manifest
    print(pretty_dumps({"valid": True, "bank_id": m["bank_id"], "family": m["family"],
                        "n_templates": m["n_templates"], "license": m["license"],
                        "data_sha256": m["data_sha256"], "signed": bool(m.get("signature"))}))  # fmt: skip
    return 0


def cmd_banks_list(_args) -> int:
    rows = [
        {"directory": str(d), "banks": sorted(p.stem for p in d.glob("*.json"))}
        for d in templates.bank_dirs()
    ]
    print(pretty_dumps(rows))
    return 0


# ---------------------------------------------------------------------------------- Monte Carlo
def cmd_mc(args) -> int:
    from .research import validation_protocol as vp

    t0 = time.perf_counter()
    truth = vp.compute_truth(args.n_truth, args.scenarios)
    results = vp.run_set("dev", args.n, truth, args.scenarios)
    summary = vp.summarize(results)
    doc = {
        "protocol_id": vp.PROTOCOL_ID,
        "set": "dev",
        "statement": "Development set: exploratory only. It is not the locked validation and does not "
        "calibrate any interval (scripts/validate_mc.py runs the locked test set).",
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "replicates_per_scenario": {k: v["n_replicates"] for k, v in summary.items()},
        "summary": summary,
        "targets": vp.evaluate_targets(summary, vp.targets()),
        "wall_s": round(time.perf_counter() - t0, 1),
    }
    _write(args.out / "dev-summary.json", pretty_dumps(doc))
    with gzip.open(args.out / "dev-replicates.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(pretty_dumps(results))
    print(f"{doc['statement']}\nTarget outcomes on this development run ({args.n} replicates per scenario):")
    for t in doc["targets"]:
        print(f"  {t['result']:<14} {t['id']}: {t.get('k')}/{t.get('n')}")
    return 0


# ---------------------------------------------------------------------------------- docs, schemas
def cmd_docs(args) -> int:
    from . import docgen

    _write(args.out / "configuration.md", docgen.configuration_markdown())
    _write(args.out / "data-dictionary.md", docgen.data_dictionary_markdown())
    _write(args.out / "config.schema.json", docgen.config_schema_text())
    print(f"wrote configuration.md, data-dictionary.md, config.schema.json to {args.out}")
    return 0


def cmd_schema(args) -> int:
    print(pretty_dumps(load_schema(args.name)))
    return 0


# ----------------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hvsr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="complete Standard + SDOF analysis of a local recording")
    a.add_argument("recording", nargs="+", type=Path)
    a.add_argument("--out", type=Path, required=True)
    a.add_argument("--request", type=Path, help="AnalysisRequest JSON (config, report, display)")
    a.add_argument("--cache", type=Path, help="science cache directory (reused when inputs are identical)")
    a.add_argument("--plots", action="store_true", help="also render SVG figures")
    _waveform_args(a)
    a.set_defaults(fn=cmd_analyze)

    r = sub.add_parser("replay", help="recompute a published analysis and compare")
    r.add_argument("analysis", nargs="+", type=Path, help="every analysis JSON of the run")
    r.add_argument("--recording", nargs="+", type=Path, required=True)
    _waveform_args(r)
    r.set_defaults(fn=cmd_replay)

    b = sub.add_parser("banks", help="generate, validate, and list template banks")
    bsub = b.add_subparsers(dest="banks_cmd", required=True)
    bb = bsub.add_parser("build")
    bb.add_argument("--family", choices=["sdof", "mdof2", "mdof3"], required=True)
    bb.add_argument("--conditioning", default="raw", help="'raw' or 'ko:<b>'")
    bb.add_argument("--bank-id", required=True)
    bb.add_argument("--bank-version", default="1.0.0")
    bb.add_argument("--damping", help="comma-separated damping ratios")
    bb.add_argument("--mass-ratios")
    bb.add_argument("--stiffness-ratios")
    bb.add_argument("--license", help="license statement, starting with an SPDX ID")
    bb.add_argument("--sign-key-env", help="environment variable holding an HMAC signing key")
    bb.add_argument("--key-id")
    bb.add_argument("--out", type=Path, required=True)
    bb.set_defaults(fn=cmd_banks_build)
    bv = bsub.add_parser("validate")
    bv.add_argument("manifest", type=Path)
    bv.add_argument("--hmac-key-env")
    bv.add_argument("--require-signature", action="store_true")
    bv.set_defaults(fn=cmd_banks_validate)
    bsub.add_parser("list").set_defaults(fn=cmd_banks_list)

    m = sub.add_parser("mc", help="Monte Carlo development-set experiment (exploratory)")
    m.add_argument("--n", type=int, default=10, help="replicates per scenario")
    m.add_argument("--n-truth", type=int, default=20, help="truth records per scenario")
    m.add_argument("--scenarios", nargs="*", default=None)
    m.add_argument("--out", type=Path, required=True)
    m.set_defaults(fn=cmd_mc)

    d = sub.add_parser("docs", help="write the generated configuration and data-dictionary documents")
    d.add_argument("--out", type=Path, required=True)
    d.set_defaults(fn=cmd_docs)

    s = sub.add_parser("schema", help="print a JSON Schema ('config' is the analysis request)")
    s.add_argument("name", choices=sorted(SCHEMA_FILES))
    s.set_defaults(fn=cmd_schema)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (UsageError, InputError, templates.BankError, OSError, ValueError) as exc:
        message = exc.message if isinstance(exc, InputError) else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
