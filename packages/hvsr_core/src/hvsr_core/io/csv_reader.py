"""CSV / CSV.GZ waveform reader (csv-parse-v1).

Supported layouts (spec 2.1), all with a single header row and optional '#' comment lines:

- ``elapsed_time``: time column in seconds since the first sample + three components.
- ``utc_timestamps``: ISO 8601 timestamps with explicit offset + three components.
- ``samples_with_rate``: three component columns; the sampling rate MUST be declared.

Parsing contract: component values are decimal text converted with correctly rounded IEEE-754
binary64 conversion (Python ``float``). If every component value is an integer literal within int32
range the canonical dtype is int32. The decimal formatting of the original text is not part of the
canonical numerical contract. Decimal commas are not supported. Missing/non-numeric values are
errors (no fill). Time zones, units, and component identities are never guessed.
"""

from __future__ import annotations

import csv
import io
import math
import re
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from ..model import (
    ChannelMeta,
    Gap,
    InputError,
    Recording,
    RecordingDiagnostics,
    Segment,
    iso_to_ns,
)
from .limits import InputLimits, is_gzip, safe_gunzip

Layout = Literal["elapsed_time", "utc_timestamps", "samples_with_rate"]

_N = {"n", "north", "ns", "hhn", "bhn", "ehn", "hnn", "shn", "lhn", "chn", "dpn"}
_E = {"e", "east", "ew", "hhe", "bhe", "ehe", "hne", "she", "lhe", "che", "dpe"}
_Z = {"z", "vertical", "up", "ud", "v", "hhz", "bhz", "ehz", "hnz", "shz", "lhz", "chz", "dpz"}
_ONE = {"1", "h1", "hh1", "bh1", "eh1", "hn1", "sh1", "ch1", "dp1"}
_TWO = {"2", "h2", "hh2", "bh2", "eh2", "hn2", "sh2", "ch2", "dp2"}
_ELAPSED = {"time", "time_s", "t", "elapsed", "elapsed_s", "seconds", "t_s"}
_UTC = {"time", "time_utc", "timestamp", "datetime", "utc", "date_time"}
_UNIT_RE = re.compile(r"^\s*(?P<name>[^\[\(]+?)\s*[\[\(](?P<unit>[^\]\)]*)[\]\)]\s*$")


@dataclass(frozen=True)
class CSVOptions:
    layout: Layout
    column_map: dict[str, str] | None = None
    sampling_rate_hz: float | None = None
    start_time_utc: str | None = None
    timestamps_declared_utc: bool = False
    time_column: str | None = None
    timing_policy: Literal["strict", "nominal_rate"] = "strict"
    nominal_jitter_tolerance_fraction: float = 0.5
    delimiter: str | None = None


@dataclass
class CSVParseInfo:
    header: list[str]
    delimiter: str
    comment_lines: list[str]
    time_column: str | None
    component_columns: dict[str, str]
    extra_columns: list[str]
    header_units: dict[str, str]
    integer_literals: bool
    max_significant_digits: int
    row_count: int
    jitter_within_tolerance_nonzero: bool = False
    raw_bytes_were_gzip: bool = False
    notes: list[str] = field(default_factory=list)


def _norm(h: str) -> tuple[str, str | None]:
    m = _UNIT_RE.match(h)
    if m:
        return m.group("name").strip().lower(), m.group("unit").strip()
    return h.strip().lower(), None


def _detect_delimiter(header_line: str, declared: str | None) -> str:
    if declared is not None:
        return declared
    counts = {d: header_line.count(d) for d in (",", ";", "\t")}
    best = max(counts, key=lambda d: counts[d])
    if counts[best] == 0:
        raise InputError(
            "csv_delimiter", "could not detect a delimiter (comma, semicolon, or tab) in the header row"
        )
    if sorted(counts.values())[-2] == counts[best]:
        raise InputError("csv_delimiter", "ambiguous delimiter in header row; declare it explicitly")
    return best


def _sig_digits(text: str) -> int:
    t = text.strip().lstrip("+-")
    mant = re.split(r"[eE]", t, maxsplit=1)[0].replace(".", "")
    mant = mant.lstrip("0")
    return len(mant.rstrip("0")) if "." not in t else len(mant)


def map_components(
    header: list[str], options: CSVOptions, time_col: str | None
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({"N": col, "E": col, "Z": col}, {"N": "NEZ"|"12Z_mapped"})."""
    if options.column_map:
        cm = dict(options.column_map)
        if set(cm) != {"N", "E", "Z"}:
            raise InputError("column_map", "column_map must assign exactly N, E and Z")
        missing = [c for c in cm.values() if c not in header]
        if missing:
            raise InputError("column_map", f"mapped columns not found in header: {missing}")
        if len(set(cm.values())) != 3:
            raise InputError("column_map", "column_map assigns the same column to more than one component")
        kinds = {}
        for comp, col in cm.items():
            name, _ = _norm(col)
            kinds[comp] = "12Z_mapped" if name in _ONE | _TWO else "NEZ"
        return cm, kinds
    found: dict[str, list[str]] = {"N": [], "E": [], "Z": [], "1": [], "2": []}
    for col in header:
        if col == time_col:
            continue
        name, _ = _norm(col)
        for key, names in (("N", _N), ("E", _E), ("Z", _Z), ("1", _ONE), ("2", _TWO)):
            if name in names:
                found[key].append(col)
    if found["1"] or found["2"]:
        raise InputError(
            "components_require_mapping",
            f"horizontal channels labelled 1/2 ({found['1'] + found['2']}) require an explicit column_map with their orientation",
        )
    for comp in ("N", "E", "Z"):
        if len(found[comp]) == 0:
            raise InputError(
                "missing_component", f"no column recognised as component {comp}; provide column_map"
            )
        if len(found[comp]) > 1:
            raise InputError(
                "ambiguous_component", f"several columns could be component {comp}: {found[comp]}"
            )
    return {c: found[c][0] for c in ("N", "E", "Z")}, {c: "NEZ" for c in ("N", "E", "Z")}


_LINE_BREAK = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def iter_lines(text: str) -> Iterator[str]:
    """Lazy ``text.splitlines()``: same line boundaries, without holding every line in memory."""
    pos = 0
    for m in _LINE_BREAK.finditer(text):
        yield text[pos : m.start()]
        pos = m.end()
    if pos < len(text):
        yield text[pos:]


def _checked_rows(lines: Iterable[str], delim: str, offset: int):
    """csv rows; a csv-module error (e.g. an oversized field) becomes an InputError with its line."""
    reader = csv.reader(lines, delimiter=delim)
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            raise InputError("csv_syntax", f"line {offset + reader.line_num}: {exc}") from None
        yield row


def read_csv(
    data: bytes, options: CSVOptions, limits: InputLimits | None = None, filename: str = "upload.csv"
) -> tuple[Recording, CSVParseInfo]:
    limits = limits or InputLimits()
    if len(data) > limits.max_request_bytes:
        raise InputError("request_size_limit", "file exceeds the per-request byte limit")
    was_gzip = is_gzip(data)
    raw = safe_gunzip(data, limits) if was_gzip else data
    if len(raw) > limits.max_decoded_bytes:
        raise InputError("decoded_size_limit", "decoded CSV exceeds the configured limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(
            "csv_encoding", f"CSV must be UTF-8 text ({exc.reason} at byte {exc.start})"
        ) from exc

    del raw
    lines = iter_lines(text)
    comments: list[str] = []
    idx = 0
    header_line = None
    for line in lines:
        if line.startswith("#") or not line.strip():
            comments.append(line)
            idx += 1
            continue
        header_line = line
        break
    if header_line is None:
        raise InputError("csv_empty", "CSV has no header row")
    delim = _detect_delimiter(header_line, options.delimiter)
    header = next(_checked_rows([header_line], delim, idx))
    header = [h.strip() for h in header]
    if len(set(header)) != len(header):
        raise InputError("csv_header", "duplicate column names in header")

    time_col = None
    if options.layout in ("elapsed_time", "utc_timestamps"):
        if options.time_column:
            if options.time_column not in header:
                raise InputError("time_column", f"time column {options.time_column!r} not found")
            time_col = options.time_column
        else:
            names = _ELAPSED if options.layout == "elapsed_time" else _UTC
            cands = [h for h in header if _norm(h)[0] in names]
            if len(cands) != 1:
                raise InputError(
                    "time_column",
                    f"cannot identify a unique time column (candidates {cands}); declare time_column",
                )
            time_col = cands[0]
    elif options.sampling_rate_hz is None:
        raise InputError(
            "sampling_rate_required", "layout samples_with_rate requires an explicitly declared sampling rate"
        )

    comp_cols, kinds = map_components(header, options, time_col)
    col_index = {h: i for i, h in enumerate(header)}
    extra = [h for h in header if h != time_col and h not in comp_cols.values()]
    header_units = {}
    for comp, col in comp_cols.items():
        _, unit = _norm(col)
        if unit:
            header_units[comp] = unit

    reader = _checked_rows(lines, delim, idx + 1)
    comp_vals = {c: array("d") for c in ("N", "E", "Z")}
    elapsed = options.layout == "elapsed_time"
    time_text: list[str] = []
    time_vals = array("d")
    time_error: ValueError | None = None
    integer = True
    max_digits = 0
    rows = 0
    ci = {c: col_index[comp_cols[c]] for c in ("N", "E", "Z")}
    ti = col_index[time_col] if time_col else None
    ncol = len(header)
    for lineno, row in enumerate(reader, start=idx + 2):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        if row[0].startswith("#"):
            comments.append(delim.join(row))
            continue
        if len(row) != ncol:
            raise InputError("csv_row_length", f"line {lineno}: expected {ncol} fields, found {len(row)}")
        rows += 1
        if rows > limits.max_samples_per_channel:
            raise InputError(
                "sample_limit", f"more than {limits.max_samples_per_channel} samples per channel"
            )
        for comp in ("N", "E", "Z"):
            s = row[ci[comp]].strip()
            if not s:
                raise InputError(
                    "missing_value", f"line {lineno}: empty value for component {comp}; gaps are never filled"
                )
            if "," in s:
                raise InputError("decimal_comma", f"line {lineno}: decimal commas are not supported")
            try:
                v = float(s)
            except ValueError as exc:
                raise InputError(
                    "non_numeric", f"line {lineno}: non-numeric value {s!r} for component {comp}"
                ) from exc
            if not math.isfinite(v):
                raise InputError("non_finite", f"line {lineno}: non-finite value {s!r} for component {comp}")
            if integer and ("." in s or "e" in s or "E" in s or abs(v) > 2**31 - 1):
                integer = False
            if len(s) > 15:
                max_digits = max(max_digits, _sig_digits(s))
            comp_vals[comp].append(v)
        if ti is not None and elapsed:
            # A parse failure is reported after all rows, as for timestamps (first failure wins).
            try:
                time_vals.append(float(row[ti].strip()))
            except ValueError as exc:
                time_error = time_error or exc
                time_vals.append(math.nan)
        elif ti is not None:
            time_text.append(row[ti].strip())
    del text, lines, reader
    if rows < 2:
        raise InputError("csv_too_short", "CSV must contain at least two samples")
    if max_digits == 0:
        max_digits = 15  # upper bound: no component value text is longer than 15 characters

    data_arr = np.vstack([np.frombuffer(comp_vals[c], dtype=np.float64) for c in ("N", "E", "Z")])
    del comp_vals
    info = CSVParseInfo(
        header=header,
        delimiter=delim,
        comment_lines=comments,
        time_column=time_col,
        component_columns=comp_cols,
        extra_columns=extra,
        header_units=header_units,
        integer_literals=integer,
        max_significant_digits=max_digits,
        row_count=rows,
        raw_bytes_were_gzip=was_gzip,
    )
    diag = RecordingDiagnostics(timing_policy=options.timing_policy)

    if options.layout == "samples_with_rate":
        fs = float(options.sampling_rate_hz)  # type: ignore[arg-type]
        t0 = iso_to_ns(options.start_time_utc) if options.start_time_utc else None
        segments = [Segment(0, 0.0, data_arr)]
        fs_source = "declared"
        t0_source = "declared" if t0 is not None else "unknown"
    else:
        t_ns = _elapsed_ns(time_vals, time_error) if elapsed else _time_ns(time_text, options)
        fs, fs_source, segments, t0_rel = _segment_by_time(t_ns, data_arr, options, diag, info)
        if options.layout == "utc_timestamps":
            t0, t0_source = int(t_ns[0]), "data"
        else:
            t0 = iso_to_ns(options.start_time_utc) + int(t_ns[0]) if options.start_time_utc else None
            t0_source = "declared" if t0 is not None else "unknown"
        del t0_rel

    if fs <= 0 or not math.isfinite(fs):
        raise InputError("sampling_rate", "invalid sampling rate")
    duration = sum(s.n for s in segments) / fs
    if duration > limits.max_duration_s:
        raise InputError("duration_limit", f"recording longer than {limits.max_duration_s} s")

    chans = tuple(
        ChannelMeta(
            component=c,  # type: ignore[arg-type]
            source_label=comp_cols[c],
            label_kind=kinds[c],  # type: ignore[arg-type]
            units=header_units.get(c),
        )
        for c in ("N", "E", "Z")
    )
    rec = Recording(
        sampling_rate_hz=fs,
        t0_utc_ns=t0,
        t0_source=t0_source,  # type: ignore[arg-type]
        channels=chans,  # type: ignore[arg-type]
        segments=segments,
        sampling_rate_source=fs_source,  # type: ignore[arg-type]
        sample_dtype="int32" if integer else "float64",
        diagnostics=diag,
        source={"format": "csv.gz" if was_gzip else "csv", "layout": options.layout, "filename": filename},
    )
    if any(k == "12Z_mapped" for k in kinds.values()):
        diag.warnings.append(
            "horizontal channels 1/2 were mapped manually to N/E; RMS combination is rotation-invariant, geometric combination is not"
        )
    if extra:
        diag.warnings.append(f"extra columns not used for analysis: {extra}")
    return rec, info


def _elapsed_ns(values: array, parse_error: ValueError | None) -> np.ndarray:
    if parse_error is not None:
        raise InputError("time_parse", f"non-numeric elapsed time: {parse_error}") from parse_error
    t = np.frombuffer(values, dtype=np.float64)
    if not np.all(np.isfinite(t)):
        raise InputError("time_parse", "non-finite elapsed time")
    return np.rint(t * 1e9).astype(np.int64)


def _time_ns(values: list[str], options: CSVOptions) -> np.ndarray:
    """UTC timestamp text -> int64 ns."""
    suffixes = {
        v[-6:] if (len(v) > 6 and v[-6] in "+-" and v[-3] == ":") else ("Z" if v.endswith(("Z", "z")) else "")
        for v in values
    }
    if suffixes == {""}:
        if not options.timestamps_declared_utc:
            raise InputError(
                "timezone_ambiguous",
                "timestamps have no UTC offset; declare explicitly that they are UTC (timestamps_declared_utc) or add 'Z'",
            )
        return np.asarray([iso_to_ns(v + "Z") for v in values], dtype=np.int64)
    if "" in suffixes:
        raise InputError("timezone_mixed", "some timestamps have an explicit offset and others do not")
    if len(suffixes) == 1:
        suf = next(iter(suffixes))
        strip = 1 if suf == "Z" else 6
        try:
            arr = np.array([v[:-strip].replace(" ", "T") for v in values], dtype="datetime64[ns]").astype(
                np.int64
            )
        except ValueError as exc:
            raise InputError("time_parse", f"invalid timestamp: {exc}") from exc
        if suf != "Z":
            off = (int(suf[1:3]) * 3600 + int(suf[4:6]) * 60) * 1_000_000_000
            arr = arr - off if suf[0] == "+" else arr + off
        return arr
    return np.asarray([iso_to_ns(v) for v in values], dtype=np.int64)


def _segment_by_time(
    t_ns: np.ndarray, data: np.ndarray, options: CSVOptions, diag: RecordingDiagnostics, info: CSVParseInfo
):
    d = np.diff(t_ns)
    if np.any(d <= 0):
        bad = int(np.flatnonzero(d <= 0)[0])
        raise InputError("time_not_increasing", f"timestamps not strictly increasing at data row {bad + 2}")
    if options.sampling_rate_hz is not None:
        fs = float(options.sampling_rate_hz)
        source = "declared"
    else:
        med = float(np.median(d))
        fs = 1e9 / med
        snapped = round(fs, 6)
        if abs(snapped - fs) <= 1e-9 * fs:
            fs = snapped
        source = "timestamps"
        info.notes.append(f"sampling rate derived from the median timestamp interval: {fs} Hz")
    dt_ns = 1e9 / fs
    if np.any(d < 0.5 * dt_ns):
        raise InputError(
            "time_duplicate",
            "timestamp interval shorter than half a sample (duplicate or overlapping samples)",
        )
    breaks = np.flatnonzero(d > 1.5 * dt_ns)
    starts = np.concatenate(([0], breaks + 1))
    stops = np.concatenate((breaks + 1, [t_ns.size]))
    if options.timing_policy == "strict":
        tol_ns = max(1000.0, 1e-3 * dt_ns)
    else:
        if not 0 < options.nominal_jitter_tolerance_fraction <= 0.5:
            raise InputError("jitter_tolerance", "nominal_jitter_tolerance_fraction must be in (0, 0.5]")
        tol_ns = options.nominal_jitter_tolerance_fraction * dt_ns
    segments: list[Segment] = []
    max_jit = 0.0
    t_first = int(t_ns[0])
    for s, e in zip(starts, stops, strict=True):
        k = np.arange(e - s)
        resid = (t_ns[s:e] - t_ns[s]) - k * dt_ns
        jit = float(np.max(np.abs(resid))) if resid.size else 0.0
        max_jit = max(max_jit, jit)
        offset_ns = int(t_ns[s]) - t_first
        idx = int(round(offset_ns / dt_ns))
        if abs(offset_ns - idx * dt_ns) > tol_ns:
            diag.warnings.append(f"segment starting at row {s + 2} is not on the first segment's sample grid")
        segments.append(Segment(idx, offset_ns / 1e9, np.ascontiguousarray(data[:, s:e])))
        if s > 0:
            gap_s = (int(t_ns[s]) - int(t_ns[s - 1])) / 1e9 - 1.0 / fs
            diag.gaps.append(Gap(after_index=segments[-2].stop_index - 1, gap_s=gap_s))
    diag.max_timing_jitter_s = max_jit / 1e9
    if max_jit > tol_ns:
        diag.timing_irregular = True
        diag.errors.append(
            f"irregular timing: max deviation from a regular {fs:g} Hz grid is {max_jit / 1e9:.3g} s "
            f"(tolerance {tol_ns / 1e9:.3g} s). Samples are never resampled automatically."
        )
    elif max_jit > 0:
        info.jitter_within_tolerance_nonzero = True
    if diag.gaps:
        diag.warnings.append(
            f"{len(diag.gaps)} gap(s) detected; windows never span gaps and gaps are not interpolated"
        )
    return fs, source, segments, t_first


def write_normalized_csv(rec: Recording) -> bytes:
    """Normalized waveform CSV (UTF-8): segment, sample index, time since start, UTC time, N, E, Z."""
    out = io.StringIO()
    units = {c.component: (c.units or "unknown") for c in rec.channels}
    out.write(
        f"# normalized waveform export; sampling_rate_hz={rec.sampling_rate_hz!r}; units N={units['N']} E={units['E']} Z={units['Z']}\n"
    )
    out.write("segment,sample_index,time_s,time_utc,N,E,Z\n")
    for si, seg in enumerate(rec.segments):
        k = np.arange(seg.n)
        idx = seg.start_index + k
        t_rel = seg.start_offset_s + k / rec.sampling_rate_hz
        if rec.t0_utc_ns is not None:
            abs_ns = rec.t0_utc_ns + np.rint(t_rel * 1e9).astype(np.int64)
            t_abs = np.datetime_as_string(abs_ns.astype("datetime64[ns]"), unit="ns", timezone="UTC").tolist()
        else:
            t_abs = [""] * seg.n
        if rec.sample_dtype == "int32":
            cols = [list(map(str, seg.data[c].astype(np.int64).tolist())) for c in range(3)]
        else:
            cols = [list(map(repr, seg.data[c].tolist())) for c in range(3)]
        t_txt = list(map(repr, t_rel.tolist()))
        idx_txt = list(map(str, idx.tolist()))
        out.write(
            "".join(
                f"{si},{i},{t},{ta},{n},{e},{z}\n"
                for i, t, ta, n, e, z in zip(idx_txt, t_txt, t_abs, cols[0], cols[1], cols[2], strict=True)
            )
        )
    return out.getvalue().encode("utf-8")
