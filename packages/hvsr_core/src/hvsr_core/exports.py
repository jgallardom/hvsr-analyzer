"""Independent numerical exports (spec 13.3) and their machine-readable data dictionary.

CSV contract (every table below): UTF-8 without a byte-order mark, comma separator, '.' decimal
point, exactly one header row, no comment lines, '\\n' line endings. Floats use Python's shortest
round-trip representation, so a value parsed from a CSV equals the JSON value bit for bit. Booleans
are `true`/`false`. An unavailable number is an EMPTY cell and the row's `reason` column says why;
zero is never used as a placeholder (a `count` of 0 is a real count).

Per-window tables are built from the in-memory scientific result (`Science`) once, at computation
time, and cached with the science documents (`science_supplement`). They list selected AND rejected
candidates. Per-window spectra are regenerated on request from the stored recording (deterministic,
bounded by the compute limits) instead of being stored for every run.
"""

from __future__ import annotations

import csv
import gzip
import io
import math
import re
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

import numpy as np

from .matching import greedy_select
from .model import Recording, iso_to_ns, ns_to_iso
from .uncertainty import window_ln_peak
from .versions import EXPORT_FORMAT_VERSION, SCHEMA_VERSIONS

if TYPE_CHECKING:
    from .analysis import Science, SelectionResult
    from .core import CoreRun

CSV_CONVENTIONS = {
    "encoding": "UTF-8 without byte-order mark",
    "delimiter": ",",
    "decimal_separator": ".",
    "header_rows": 1,
    "comment_lines": "none",
    "line_terminator": "\\n",
    "booleans": "true / false",
    "floats": "shortest round-trip decimal representation (parses to the identical binary64 value)",
    "unavailable": "empty cell; the row's `reason` column states why. Zero is never a placeholder.",
    "times": "UTC ISO 8601 with a trailing Z; offsets in seconds from the first canonical sample",
}

MEAN_STATUS = ("ok", "ci_limited", "ci_unavailable", "mean_unavailable", "frequency_invalid")
STANDARD_OUTCOMES = ("selected", "qc_rejected", "overlap_excluded", "fdwra_rejected")
SDOF_OUTCOMES = ("selected", "qc_rejected", "not_eligible", "overlap_excluded", "beyond_requested_n")
SDOF_ELIGIBILITY = (
    "qc_rejected",
    "eligible",
    "no_meaningful_match",
    "no_peak_in_family_band",
    "incomplete_fit_band",
    "outside_common_band",
    "insufficient_resolution",
    "undefined_r2",
)


def _col(name, type_, unit, description, nullable=False, enum=None) -> dict:
    d = {"name": name, "type": type_, "unit": unit, "nullable": nullable, "description": description}
    if enum is not None:
        d["enum"] = list(enum)
    return d


_REASON = _col(
    "reason",
    "string",
    None,
    "Why each empty numeric cell in this row is unavailable ('column: reason', separated by ' | '). "
    "Empty when every value is present.",
    nullable=True,
)
_WINDOW_COLUMNS = [
    _col("window_id", "string", None, "Stable candidate window ID derived from the start sample."),
    _col("start_sample", "integer", "samples", "Window start, samples from the first canonical sample."),
    _col("start_s", "number", "s", "Window start, seconds from the first canonical sample."),
    _col("end_s", "number", "s", "Window end (exclusive), seconds from the first canonical sample."),
    _col(
        "start_utc",
        "string",
        "UTC ISO 8601",
        "Absolute window start; empty when the recording has no absolute start time.",
        nullable=True,
    ),
    _col("segment_index", "integer", None, "Contiguous recording segment containing the window."),
    _col("qc_passed", "boolean", None, "Basic window quality control passed."),
    _col(
        "qc_reasons",
        "string",
        None,
        "Basic-QC rejection reasons separated by ';' (empty when passed).",
        nullable=True,
    ),
]

TABLES: dict[str, dict] = {
    "mean_spectrum": {
        "title": "Mean H/V spectrum",
        "row": "one selection (method / peak family) at one frequency of the analysis grid",
        "description": "Selected-window geometric-mean H/V with the single nominal 95% pointwise "
        "band ('Estimated 95% pointwise CI for the selected-window mean'), exactly as in the analysis "
        "JSON `selections[].mean_curve`. The band is selection-conditional and its coverage is not "
        "calibrated unless the analysis JSON `calibration.status` says otherwise.",
        "columns": [
            _col("selection_id", "string", None, "Selection identifier, as in the analysis JSON."),
            _col(
                "peak_family_id",
                "string",
                None,
                "Peak family of the selection; empty when there is none.",
                nullable=True,
            ),
            _col("frequency_hz", "number", "Hz", "Frequency of the anchored logarithmic analysis grid."),
            _col(
                "hv_geometric_mean",
                "number",
                "dimensionless (H/V)",
                "Geometric mean of the selected windows' H/V (ln-amplitude average).",
                nullable=True,
            ),
            _col(
                "ci95_lower",
                "number",
                "dimensionless (H/V)",
                "Lower bound of the Estimated 95% pointwise CI for the selected-window mean.",
                nullable=True,
            ),
            _col(
                "ci95_upper",
                "number",
                "dimensionless (H/V)",
                "Upper bound of the Estimated 95% pointwise CI for the selected-window mean.",
                nullable=True,
            ),
            _col("count", "integer", "windows", "Selected windows valid at this frequency."),
            _col(
                "log_std",
                "number",
                "natural-log units of H/V",
                "Sample standard deviation of ln H/V across the selected windows (dispersion, not the CI).",
                nullable=True,
            ),
            _col("frequency_valid", "boolean", None, "The analysis grid is valid at this frequency."),
            _col("status", "string", None, "Row status.", enum=MEAN_STATUS),
            _REASON,
        ],
    },
    "window_table_standard": {
        "title": "Per-window table, Standard method",
        "row": "one candidate window of one Standard selection (every candidate, selected or not)",
        "description": "Candidate windows with basic-QC result and the Standard rule outcome "
        "(earliest-first non-overlapping windows; optional FDWRA).",
        "columns": [
            _col("selection_id", "string", None, "Selection identifier, as in the analysis JSON."),
            *_WINDOW_COLUMNS,
            _col("outcome", "string", None, "Outcome of the Standard rule.", enum=STANDARD_OUTCOMES),
            _col(
                "selection_order",
                "integer",
                None,
                "1-based position in the selection (time order).",
                nullable=True,
            ),
            _col(
                "peak_family_id",
                "string",
                None,
                "Peak family used for the per-window peak; empty when there is none.",
                nullable=True,
            ),
            _col(
                "window_peak_frequency_hz",
                "number",
                "Hz",
                "Strict in-family peak of this window's H/V (parabolic refinement).",
                nullable=True,
            ),
            _REASON,
        ],
    },
    "window_table_sdof": {
        "title": "Per-window table, SDOF-selected method",
        "row": "one candidate window of one peak family (every candidate, selected or not)",
        "description": "Peak-aligned SDOF template matching (R2_log on the 129-point ln-q grid), "
        "eligibility, ranking, and greedy non-overlapping selection inside each plausible peak family.",
        "columns": [
            _col("selection_id", "string", None, "Selection identifier, as in the analysis JSON."),
            _col("peak_family_id", "string", None, "Peak family in which the window was ranked."),
            *_WINDOW_COLUMNS,
            _col("eligibility", "string", None, "Matching eligibility status.", enum=SDOF_ELIGIBILITY),
            _col("outcome", "string", None, "Selection outcome.", enum=SDOF_OUTCOMES),
            _col(
                "window_peak_frequency_hz",
                "number",
                "Hz",
                "Bank-independent in-family peak frequency fp of this window.",
                nullable=True,
            ),
            _col(
                "window_peak_amplitude",
                "number",
                "dimensionless (H/V)",
                "H/V amplitude Ap at fp.",
                nullable=True,
            ),
            _col("fit_band_lower_hz", "number", "Hz", "Template fit band lower edge fp/2.", nullable=True),
            _col("fit_band_upper_hz", "number", "Hz", "Template fit band upper edge 2 fp.", nullable=True),
            _col(
                "r2_log",
                "number",
                "dimensionless",
                "Best-template R2_log (template resemblance, not a quality probability).",
                nullable=True,
            ),
            _col(
                "r2_rank",
                "integer",
                None,
                "1-based rank among eligible windows: descending R2_log, then earlier start, then window ID.",
                nullable=True,
            ),
            _col(
                "selection_order",
                "integer",
                None,
                "1-based position in the greedy selection order.",
                nullable=True,
            ),
            _col("template_index", "integer", None, "Best template index in the bank.", nullable=True),
            _col(
                "template_parameters",
                "string",
                None,
                "Best template parameters 'name=value' separated by '; '.",
                nullable=True,
            ),
            _col("flags", "string", None, "Matching flags separated by ';'.", nullable=True),
            _REASON,
        ],
    },
    "window_spectra": {
        "title": "Per-window H/V spectra",
        "row": "one candidate window at one valid frequency of the analysis grid",
        "description": "Smoothed H/V of every candidate window (selected or not) at the frequencies where "
        "the analysis grid is valid (the full grid and its validity are in the analysis JSON). "
        "Regenerated deterministically from the stored recording and the run configuration.",
        "columns": [
            _col("window_id", "string", None, "Stable candidate window ID."),
            _col(
                "start_sample", "integer", "samples", "Window start, samples from the first canonical sample."
            ),
            _col("frequency_hz", "number", "Hz", "Analysis grid frequency."),
            _col("hv", "number", "dimensionless (H/V)", "Window H/V amplitude.", nullable=True),
            _REASON,
        ],
    },
    "normalized_waveform": {
        "title": "Normalized waveform",
        "row": "one sample instant (gaps appear as jumps in sample_index; nothing is interpolated)",
        "description": "Three-component samples exactly as parsed (integers for integer data, "
        "shortest round-trip decimals for floating data). Readable without decoding MiniSEED.",
        "columns": [
            _col("sample_index", "integer", "samples", "Index from the first canonical sample."),
            _col("time_offset_s", "number", "s", "sample_index divided by the sampling rate."),
            _col(
                "time_utc",
                "string",
                "UTC ISO 8601",
                "Absolute sample time (nanosecond digits). Present only when the recording has an "
                "absolute start time.",
            ),
        ],
        "optional_columns": ["time_utc"],
        "dynamic_columns": [
            {
                "pattern": f"^{c}_[A-Za-z0-9_]+$",
                "type": "number",
                "description": f"{c} component sample; the header suffix is the declared units "
                "('/' as '_per_', 'unknown_units' when undeclared).",
            }
            for c in ("N", "E", "Z")
        ],
    },
}


# ------------------------------------------------------------------------------------- CSV writing
def _cell(v, type_: str) -> str:
    if v is None:
        return ""
    if type_ == "boolean":
        return "true" if v else "false"
    if type_ == "integer":
        return str(int(v))
    if type_ == "number":
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("non-finite number in a CSV export")
        return repr(f)
    return str(v)


def table_csv(table: str, rows: Iterable[dict]) -> bytes:
    cols = TABLES[table]["columns"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([c["name"] for c in cols])
    for r in rows:
        w.writerow([_cell(r.get(c["name"]), c["type"]) for c in cols])
    return buf.getvalue().encode("utf-8")


def gzip_bytes(data: bytes) -> bytes:
    """Deterministic gzip (no file name, mtime 0)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=6) as gz:
        gz.write(data)
    return buf.getvalue()


def _reason(parts: list[str]) -> str | None:
    return " | ".join(parts) or None


# ------------------------------------------------------------------------------------ CSV validation
def read_csv_table(data: bytes) -> tuple[list[str], list[list[str]]]:
    text = data.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


def _header_columns(header: list[str], spec: dict) -> tuple[list[dict], list[str]]:
    fixed = {c["name"]: c for c in spec["columns"]}
    optional = set(spec.get("optional_columns", []))
    dyn = spec.get("dynamic_columns", [])
    cols, errs = [], []
    for name in header:
        if name in fixed:
            cols.append(fixed[name])
            continue
        hit = next((d for d in dyn if re.match(d["pattern"], name)), None)
        if hit is None:
            errs.append(f"unexpected column {name!r}")
            cols.append({"name": name, "type": "string", "nullable": True})
        else:
            cols.append({"name": name, "type": hit["type"], "nullable": False})
    expected = [c["name"] for c in spec["columns"] if c["name"] not in optional or c["name"] in header]
    if [h for h in header if h in fixed] != expected:
        errs.append(f"header {header} does not match the dictionary columns {expected}")
    for d in dyn:
        if sum(bool(re.match(d["pattern"], h)) for h in header) != 1:
            errs.append(f"exactly one column must match {d['pattern']}")
    return cols, errs


def validate_csv(data: bytes, table: str, max_errors: int = 50) -> list[str]:
    """Check a CSV export against its data-dictionary entry. Returns problems (empty when valid)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return ["byte-order mark present"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"not UTF-8: {exc}"]
    if "\r" in text:
        return ["carriage return in line endings"]
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ["empty file"]
    header, body = rows[0], rows[1:]
    cols, errs = _header_columns(header, TABLES[table])
    ri = header.index("reason") if "reason" in header else None
    for n, row in enumerate(body, start=2):
        if len(errs) >= max_errors:
            break
        if len(row) != len(header):
            errs.append(f"line {n}: {len(row)} cells, header has {len(header)}")
            continue
        blank_numbers = []
        for v, c in zip(row, cols, strict=True):
            if v == "":
                if not c.get("nullable"):
                    errs.append(f"line {n}: {c['name']} is empty but not nullable")
                elif c["type"] in ("number", "integer"):
                    blank_numbers.append(c["name"])
                continue
            t = c["type"]
            if t == "integer" and not re.fullmatch(r"-?[0-9]+", v):
                errs.append(f"line {n}: {c['name']}={v!r} is not an integer")
            elif t == "number":
                if not re.fullmatch(r"-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?", v):
                    errs.append(f"line {n}: {c['name']}={v!r} is not a finite decimal number")
            elif t == "boolean" and v not in ("true", "false"):
                errs.append(f"line {n}: {c['name']}={v!r} is not true/false")
            if "enum" in c and v not in c["enum"]:
                errs.append(f"line {n}: {c['name']}={v!r} not in {c['enum']}")
        if blank_numbers and (ri is None or not row[ri]):
            errs.append(f"line {n}: empty {blank_numbers} without a reason")
    return errs[:max_errors]


# ------------------------------------------------------------------------------------- mean spectrum
def mean_spectrum_rows(doc: dict, grid_invalid_reason: list[str | None]) -> list[dict]:
    """Rows of the mean-spectrum CSV, read from a published analysis document (never recomputed)."""
    freq, gvalid = doc["grid"]["frequency_hz"], doc["grid"]["valid"]
    out = []
    for sel in doc["selections"]:
        mc, unc = sel["mean_curve"], sel["uncertainty"]
        ci_status, ci_reason = unc.get("status"), unc.get("status_reason")
        for i, f in enumerate(freq):
            hv, lo, hi = mc["hv_geometric_mean"][i], mc["ci95_lower"][i], mc["ci95_upper"][i]
            ls = mc["log_std"][i]
            why: list[str] = []
            if not gvalid[i]:
                status = "frequency_invalid"
                why.append(
                    f"all values: analysis grid invalid here ({grid_invalid_reason[i] or 'unspecified'})"
                )
            elif hv is None:
                status = "mean_unavailable"
                why.append(
                    "all values: "
                    + (
                        f"no window selected ({sel['status_reason'] or sel['status']})"
                        if sel["n_selected"] == 0
                        else "no selected window is valid at this frequency"
                    )
                )
            else:
                if lo is None or hi is None:
                    status = "ci_unavailable"
                    why.append(
                        "ci95: "
                        + (
                            f"{ci_status}: {ci_reason}"
                            if ci_status == "ci_not_estimable"
                            else "not estimable at this frequency (bootstrap replicates lack valid values)"
                        )
                    )
                else:
                    status = "ci_limited" if ci_status == "ci_limited" else "ok"
                if ls is None:
                    why.append("log_std: needs at least 2 valid windows")
            out.append(
                {
                    "selection_id": sel["selection_id"],
                    "peak_family_id": sel["peak_family_id"],
                    "frequency_hz": f,
                    "hv_geometric_mean": hv,
                    "ci95_lower": lo,
                    "ci95_upper": hi,
                    "count": mc["count"][i],
                    "log_std": ls,
                    "frequency_valid": bool(gvalid[i]),
                    "status": status,
                    "reason": _reason(why),
                }
            )
    return out


# ------------------------------------------------------------------------------------ window tables
def _window_base(sci: Science) -> list[dict]:
    core = sci.core
    win, fs = core.window, core.prep.fs
    t0 = iso_to_ns(sci.rec["start_utc"]) if sci.rec["start_utc"] else None
    ids = win.ids()
    out = []
    for r in range(win.count):
        s = int(win.global_start[r])
        reasons = list(core.qc.reasons[r])
        out.append(
            {
                "window_id": ids[r],
                "start_sample": s,
                "start_s": s / fs,
                "end_s": (s + win.n_samples) / fs,
                "start_utc": None if t0 is None else ns_to_iso(t0 + round(s * 1e9 / fs)),
                "segment_index": int(core.prep.segments[int(win.segment_pos[r])].segment_index),
                "qc_passed": bool(core.qc.passed[r]),
                "qc_reasons": ";".join(reasons) or None,
            }
        )
    return out


def _with_reason(row: dict, why: list[str], no_utc: bool) -> dict:
    if no_utc:
        why = ["start_utc: the recording has no absolute start time", *why]
    row["reason"] = _reason(why)
    return row


def _standard_rows(sci: Science, s: SelectionResult, base: list[dict], selection_id: str) -> list[dict]:
    core = sci.core
    order = {int(r): k for k, r in enumerate(s.rows)}
    basic = {int(r) for r in core.standard.basic_rows}
    fam = s.family
    all_rows = np.arange(core.window.count)
    peaks = (
        np.exp(window_ln_peak(core.spectra, all_rows, fam.lo_index, fam.hi_index))
        if fam is not None
        else np.full(all_rows.size, np.nan)
    )
    out = []
    for r, b in enumerate(base):
        if not b["qc_passed"]:
            outcome = "qc_rejected"
        elif r in order:
            outcome = "selected"
        elif r in basic and s.method == "standard":
            outcome = "fdwra_rejected"
        else:
            outcome = "overlap_excluded"
        why = []
        if r not in order:
            why.append(f"selection_order: not selected ({outcome})")
        fp = float(peaks[r]) if np.isfinite(peaks[r]) else None
        if fp is None:
            why.append(
                "window_peak_frequency_hz: "
                + ("no peak family" if fam is None else f"no strict peak inside family {fam.family_id}")
            )
        row = dict(
            b,
            selection_id=selection_id,
            outcome=outcome,
            selection_order=order[r] + 1 if r in order else None,
            peak_family_id=fam.family_id if fam is not None else None,
            window_peak_frequency_hz=fp,
        )
        out.append(_with_reason(row, why, b["start_utc"] is None))
    return out


def _sdof_rows(sci: Science, s: SelectionResult, base: list[dict], selection_id: str) -> list[dict]:
    win = sci.core.window
    t = s.sdof.table
    ids = win.ids()
    pos = {int(r): k for k, r in enumerate(t.rows)}
    elig = t.eligible_mask()
    gs = win.global_start[t.rows]
    tids = [ids[r] for r in t.rows]
    greedy = {
        int(t.rows[i])
        for i in greedy_select(gs, tids, win.n_samples, np.nan_to_num(t.best_r2, nan=-np.inf), elig)
    }
    ranked = sorted(np.flatnonzero(elig).tolist(), key=lambda i: (-t.best_r2[i], int(gs[i]), tids[i]))
    rank = {int(t.rows[i]): n + 1 for n, i in enumerate(ranked)}
    order = {int(r): k for k, r in enumerate(s.rows)}
    out = []
    for r, b in enumerate(base):
        k = pos.get(r)
        why: list[str] = []
        vals: dict = dict.fromkeys(
            (
                "window_peak_frequency_hz",
                "window_peak_amplitude",
                "fit_band_lower_hz",
                "fit_band_upper_hz",
                "r2_log",
                "template_index",
            )
        )
        tpl = flags = None
        if k is None:
            eligibility = outcome = "qc_rejected"
            why.append("all matching values: window failed basic QC and was not matched")
        else:
            eligibility = t.status[k]
            if r in order:
                outcome = "selected"
            elif eligibility != "eligible":
                outcome = "not_eligible"
            elif r in greedy:
                outcome = "beyond_requested_n"
            else:
                outcome = "overlap_excluded"
            if np.isfinite(t.peaks.fp[k]):  # the fit band [fp/2, 2 fp] exists exactly when fp does
                vals["window_peak_frequency_hz"] = float(t.peaks.fp[k])
                vals["window_peak_amplitude"] = float(math.exp(t.peaks.ln_ap[k]))
                vals["fit_band_lower_hz"] = float(t.fit_band_hz[k, 0])
                vals["fit_band_upper_hz"] = float(t.fit_band_hz[k, 1])
            else:
                why.append(f"window peak and fit band: no strict peak inside family {t.family_id}")
            if t.best_k[k] >= 0 and np.isfinite(t.best_r2[k]):
                vals["r2_log"] = float(t.best_r2[k])
                vals["template_index"] = int(t.best_k[k])
                lab = sci.bank.template_label(int(t.best_k[k]))
                tpl = "; ".join(f"{n}={v!r}" for n, v in lab.items() if n != "template_index")
            else:
                why.append(f"r2_log, template_index: not matched ({eligibility})")
            flags = ";".join(t.flags[k]) or None
        if r not in rank:
            why.append(
                "r2_rank: ranked only among eligible windows"
                if k is not None
                else "r2_rank: not ranked (qc_rejected)"
            )
        if r not in order:
            why.append(f"selection_order: not selected ({outcome})")
        row = dict(
            b,
            selection_id=selection_id,
            peak_family_id=t.family_id,
            eligibility=eligibility,
            outcome=outcome,
            **vals,
            r2_rank=rank.get(r),
            selection_order=order[r] + 1 if r in order else None,
            template_parameters=tpl,
            flags=flags,
        )
        out.append(_with_reason(row, why, b["start_utc"] is None))
    return out


def window_tables(sci: Science) -> dict[str, list[dict]]:
    """Per-method candidate tables (every candidate; selected and rejected), keyed like the documents."""
    base = _window_base(sci)
    out = {"standard": _standard_rows(sci, sci.standard, base, "standard-all")}
    if sci.audit is not None:
        out["standard_basic_audit"] = _standard_rows(sci, sci.audit, base, "standard_basic_audit-all")
    rows = []
    for s in sci.sdof:
        rows += _sdof_rows(sci, s, base, f"sdof-{s.family.family_id}")
    out["sdof"] = rows
    return out


def science_supplement(sci: Science) -> dict:
    """Deterministic per-run data the documents do not carry; cached next to the science documents."""
    return {
        "supplement_version": EXPORT_FORMAT_VERSION,
        "science_key": sci.key,
        "grid_invalid_reason": list(sci.core.grid.invalid_reason),
        "window_tables": window_tables(sci),
    }


WINDOW_TABLE = {"standard": "window_table_standard", "standard_basic_audit": "window_table_standard"}


def window_table_name(method: str) -> str:
    return WINDOW_TABLE.get(method, "window_table_sdof")


def window_table_doc(doc: dict, rows: list[dict]) -> dict:
    """JSON form of a per-window table: the same rows and values as the CSV."""
    table = window_table_name(doc["method"])
    return {
        "schema_id": "hvsr-analyzer/window-table",
        "schema_version": SCHEMA_VERSIONS["window_table"],
        "table": table,
        "run_id": doc["run_id"],
        "method": doc["method"],
        "science_key": doc["provenance"]["science_key"],
        "record": doc["record"],
        "export_format": EXPORT_FORMAT_VERSION,
        "columns": [c["name"] for c in TABLES[table]["columns"]],
        "units": {c["name"]: c["unit"] for c in TABLES[table]["columns"] if c["unit"]},
        "ranking_order": "descending R2_log, then earlier start, then window id"
        if table == "window_table_sdof"
        else None,
        "rows": rows,
    }


# ------------------------------------------------------------------------- regenerated on request
def window_spectra_csv_gz(core: CoreRun) -> bytes:
    sd = core.spectra
    ids = sd.window.ids()
    valid_f = np.flatnonzero(sd.grid.valid)
    freqs = [repr(float(f)) for f in sd.grid.freq[valid_f]]
    names = [c["name"] for c in TABLES["window_spectra"]["columns"]]
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=6) as gz:
        gz.write((",".join(names) + "\n").encode())
        for r in range(sd.window.count):
            start = str(int(sd.window.global_start[r]))
            lines = []
            for j, i in enumerate(valid_f):
                ok = bool(sd.valid[r, i]) and np.isfinite(sd.hv[r, i])
                hv = repr(float(sd.hv[r, i])) if ok else ""
                why = "" if ok else "hv: this window's spectrum is invalid at this frequency"
                lines.append(f"{ids[r]},{start},{freqs[j]},{hv},{why}\n")
            gz.write("".join(lines).encode())
    return buf.getvalue()


def unit_token(units: str | None) -> str:
    if not units or not units.strip():
        return "unknown_units"
    s = units.strip().replace("**", "").replace("^", "").replace("/", "_per_")
    return re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_") or "unknown_units"


def normalized_waveform_header(rec: Recording) -> list[str]:
    return (
        ["sample_index", "time_offset_s"]
        + (["time_utc"] if rec.t0_utc_ns is not None else [])
        + [f"{c.component}_{unit_token(c.units)}" for c in rec.channels]
    )


def _waveform_lines(rec: Recording, chunk: int = 100_000) -> Iterator[str]:
    fs = rec.sampling_rate_hz
    is_int = rec.sample_dtype == "int32"
    for seg in rec.segments:
        for a in range(0, seg.n, chunk):
            b = min(seg.n, a + chunk)
            idx = np.arange(seg.start_index + a, seg.start_index + b, dtype=np.int64)
            cols = [idx.astype(str).tolist(), [repr(v) for v in (idx / fs).tolist()]]
            if rec.t0_utc_ns is not None:
                ns = rec.t0_utc_ns + np.round(idx * (1e9 / fs)).astype(np.int64)
                cols.append([s + "Z" for s in np.datetime_as_string(ns.astype("datetime64[ns]"), unit="ns")])
            for ci in range(3):
                vals = seg.data[ci, a:b]
                cols.append(
                    [str(v) for v in vals.astype(np.int64).tolist()]
                    if is_int
                    else [repr(v) for v in vals.tolist()]
                )
            yield "".join(",".join(r) + "\n" for r in zip(*cols, strict=True))


def normalized_waveform_csv_gz(rec: Recording) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=6) as gz:
        gz.write((",".join(normalized_waveform_header(rec)) + "\n").encode())
        for block in _waveform_lines(rec):
            gz.write(block.encode())
    return buf.getvalue()


# --------------------------------------------------------------------------------- data dictionary
def data_dictionary() -> dict:
    from .analysis import UNITS

    return {
        "schema_id": "hvsr-analyzer/data-dictionary",
        "schema_version": SCHEMA_VERSIONS["data_dictionary"],
        "export_format": EXPORT_FORMAT_VERSION,
        "csv_conventions": CSV_CONVENTIONS,
        "tables": TABLES,
        "json_documents": {
            "analysis": "Per-run, per-method analysis (schema: analysis)",
            "manifest": "Per-measurement index of every artifact (schema: manifest)",
            "window_table": "JSON form of the per-window tables (schema: window_table)",
            "plot_recipe": "Figure recipe stored with every plot artifact (schema: plot_recipe)",
            "data_dictionary": "This document (schema: data_dictionary)",
        },
        "analysis_units": UNITS,
        "categories": {
            "measured_data": "original_data, canonical_recordings, sensor_metadata",
            "algorithmic_results": "analysis_runs, spectra, plots",
            "experimental_diagnostics": "quality_evaluation (robustness comparison), calibration status",
            "human_interpretation": "annotations (none are generated automatically)",
        },
    }
