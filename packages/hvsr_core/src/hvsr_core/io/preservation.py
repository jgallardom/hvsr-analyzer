"""Original-byte preservation and verified canonical MiniSEED conversion (spec 4).

Default policy: keep the exact uploaded bytes (MiniSEED and CSV.GZ verbatim; plain CSV as a
deterministic gzip whose decompression reproduces the original byte stream and SHA-256), plus a
canonical numerical recording when useful.

Optional canonical-only policy (explicit consent, CSV sources only): keep canonical MiniSEED +
sidecar + exact text of the time/extra columns and comment lines, and drop the original CSV bytes
ONLY after the canonical data were written and verified. Downloads are then labeled
"Canonical recording" and "Reconstructed CSV"; never "Original CSV".
"""

from __future__ import annotations

import csv
import gzip
import io
from dataclasses import dataclass, field
from typing import Literal, Protocol

import numpy as np

from ..jsonutil import canonical_dumps, sha256_bytes
from ..model import InputError, Recording, ns_to_iso
from ..versions import ALGORITHM_VERSIONS, PACKAGE_VERSION
from .csv_reader import CSVParseInfo
from .limits import InputLimits, is_gzip, safe_gunzip
from .mseed_reader import detect_miniseed_version, read_miniseed

PARSING_CONTRACT = (
    "Component values are decimal text converted with correctly rounded IEEE-754 binary64 conversion "
    "(int32 when every value is an integer literal in range). The canonical arrays equal those parsed "
    "values bitwise. Decimal formatting of the original text (digits, padding, exponent style) is not "
    "part of the preservation contract."
)


@dataclass
class PreservedOriginal:
    filename: str
    detected_format: str
    original_sha256: str
    original_size: int
    logical_uncompressed_size: int
    stored_encoding: Literal["identity", "gzip"]
    stored_bytes: bytes = field(repr=False)
    stored_sha256: str = ""
    stored_size: int = 0

    def describe(self) -> dict:
        return {
            "filename": self.filename,
            "detected_format": self.detected_format,
            "original_sha256": self.original_sha256,
            "original_size": self.original_size,
            "logical_uncompressed_size": self.logical_uncompressed_size,
            "stored_encoding": self.stored_encoding,
            "stored_sha256": self.stored_sha256,
            "stored_size": self.stored_size,
        }


def detect_format(filename: str, data: bytes) -> str:
    if is_gzip(data):
        return "csv.gz"
    if detect_miniseed_version(data) in (2, 3):
        return "miniseed" if detect_miniseed_version(data) == 2 else "miniseed3"
    head = data[:512].lstrip()
    if head.startswith(b"<?xml") or head.startswith(b"<FDSNStationXML"):
        return "stationxml"
    return "csv"


def preserve_original(filename: str, data: bytes, limits: InputLimits | None = None) -> PreservedOriginal:
    limits = limits or InputLimits()
    fmt = detect_format(filename, data)
    original_sha = sha256_bytes(data)
    if fmt == "csv":
        stored = gzip.compress(data, compresslevel=9, mtime=0)
        if gzip.decompress(stored) != data:  # pragma: no cover - zlib invariant
            raise RuntimeError("gzip round trip failed")
        encoding: Literal["identity", "gzip"] = "gzip"
        logical = len(data)
    elif fmt == "csv.gz":
        stored = data
        encoding = "identity"
        logical = len(safe_gunzip(data, limits))
    else:
        stored = data
        encoding = "identity"
        logical = len(data)
    return PreservedOriginal(
        filename=filename,
        detected_format=fmt,
        original_sha256=original_sha,
        original_size=len(data),
        logical_uncompressed_size=logical,
        stored_encoding=encoding,
        stored_bytes=stored,
        stored_sha256=sha256_bytes(stored),
        stored_size=len(stored),
    )


def reconstruct_original(stored_bytes: bytes, stored_encoding: str, expected_sha256: str | None = None) -> bytes:
    out = gzip.decompress(stored_bytes) if stored_encoding == "gzip" else stored_bytes
    if expected_sha256 is not None and sha256_bytes(out) != expected_sha256:
        raise RuntimeError("reconstructed original does not match its recorded SHA-256")
    return out


# --------------------------------------------------------------------------- canonical conversion


@dataclass
class CanonicalArtifact:
    mseed_bytes: bytes = field(repr=False)
    mseed_sha256: str
    encoding: str
    sidecar: dict
    residual_gz: bytes | None = field(default=None, repr=False)
    residual_sha256: str | None = None
    verification: dict = field(default_factory=dict)

    @property
    def sidecar_bytes(self) -> bytes:
        return canonical_dumps(self.sidecar).encode("utf-8")


def _band_code(fs: float) -> str:
    if fs >= 250:
        return "D"
    if fs >= 80:
        return "H"
    if fs >= 10:
        return "B"
    if fs > 1:
        return "M"
    return "L"


def _channel_codes(rec: Recording) -> dict[str, tuple[str, str, str, str]]:
    codes = {}
    for ch in rec.channels:
        parts = ch.source_label.split(".")
        if rec.source.get("format") == "miniseed2" and len(parts) == 4:
            codes[ch.component] = tuple(parts)  # type: ignore[assignment]
            continue
        inst = {"velocity": "H", "acceleration": "N"}.get(ch.sensed_quantity, "X")
        orient = ch.component
        if ch.label_kind == "12Z_mapped":
            orient = "1" if ch.component == "N" else ("2" if ch.component == "E" else "Z")
        codes[ch.component] = ("XX", "HVSR", "", f"{_band_code(rec.sampling_rate_hz)}{inst}{orient}")
    return codes


def build_canonical(rec: Recording, original: PreservedOriginal | None = None, csv_info: CSVParseInfo | None = None, csv_bytes: bytes | None = None) -> CanonicalArtifact:
    """Write canonical MiniSEED (STEIM2 for int32 data, FLOAT64 otherwise) and verify it by reading
    it back with the production reader. Raises InputError when verification fails."""
    from obspy import Stream, Trace, UTCDateTime, __version__ as obspy_version

    codes = _channel_codes(rec)
    t0 = rec.t0_utc_ns if rec.t0_utc_ns is not None else 0
    is_int = rec.sample_dtype == "int32"

    def write(encoding: str) -> bytes:
        st = Stream()
        for seg in rec.segments:
            start_ns = t0 + round(seg.start_index * 1e9 / rec.sampling_rate_hz)
            for ci, comp in enumerate(("N", "E", "Z")):
                arr = seg.data[ci]
                if is_int:
                    data = arr.astype(np.int32)
                    if not np.array_equal(data.astype(np.float64), arr):
                        raise InputError("canonical_int_mismatch", "integer data not representable as int32")
                else:
                    data = np.ascontiguousarray(arr, dtype=np.float64)
                tr = Trace(data=data)
                net, sta, loc, cha = codes[comp]
                tr.stats.network, tr.stats.station, tr.stats.location, tr.stats.channel = net, sta, loc, cha
                tr.stats.sampling_rate = rec.sampling_rate_hz
                tr.stats.starttime = UTCDateTime(ns=start_ns)
                st.append(tr)
        buf = io.BytesIO()
        st.write(buf, format="MSEED", encoding=encoding, reclen=4096)
        return buf.getvalue()

    encodings = ["STEIM2", "INT32"] if is_int else ["FLOAT64"]
    last_err = None
    for enc in encodings:
        try:
            mseed = write(enc)
        except Exception as exc:  # noqa: BLE001 - libmseed packing errors
            last_err = exc
            continue
        verification = verify_canonical(mseed, rec, codes)
        if verification["ok"]:
            break
        last_err = RuntimeError(f"verification failed with {enc}: {verification}")
    else:
        raise InputError("canonical_conversion_failed", f"canonical MiniSEED could not be written and verified ({last_err})")

    residual_gz, residual_cols = (None, [])
    if csv_info is not None and csv_bytes is not None:
        residual_gz, residual_cols = extract_residual_columns(csv_bytes, csv_info)

    exact_t0 = rec.t0_utc_ns
    mseed_t0_exact = exact_t0 is None or exact_t0 % 1000 == 0
    sidecar = {
        "sidecar_schema": "hvsr-analyzer/canonical-sidecar",
        "sidecar_version": "1",
        "conversion": {
            "software": f"hvsr-core {PACKAGE_VERSION}",
            "algorithm": ALGORITHM_VERSIONS["canonical_conversion"],
            "writer": f"obspy {obspy_version} (libmseed), MiniSEED 2, record length 4096",
            "encoding": enc,
        },
        "source": original.describe() if original else None,
        "source_layout": rec.source.get("layout"),
        "sampling_rate_hz": rec.sampling_rate_hz,
        "sampling_rate_source": rec.sampling_rate_source,
        "start_time": {
            "exact_utc_ns": exact_t0,
            "iso": ns_to_iso(exact_t0),
            "source": rec.t0_source,
            "miniseed_start_exact": mseed_t0_exact,
            "note": None if mseed_t0_exact else "MiniSEED stores microseconds; the exact start is this sidecar value",
            "placeholder_epoch_used": rec.t0_utc_ns is None,
        },
        "segments": [{"start_index": s.start_index, "start_offset_s": s.start_offset_s, "n": s.n} for s in rec.segments],
        "channels": [dict(ch.as_dict(), miniseed_id=".".join(codes[ch.component])) for ch in rec.channels],
        "sample_dtype": rec.sample_dtype,
        "parsing_contract": PARSING_CONTRACT,
        "timing": {
            "policy": rec.diagnostics.timing_policy,
            "max_jitter_s": rec.diagnostics.max_timing_jitter_s,
            "irregular": rec.diagnostics.timing_irregular,
        },
        "csv": None
        if csv_info is None
        else {
            "header": csv_info.header,
            "delimiter": csv_info.delimiter,
            "time_column": csv_info.time_column,
            "component_columns": csv_info.component_columns,
            "extra_columns": csv_info.extra_columns,
            "comment_line_count": len(csv_info.comment_lines),
            "max_significant_digits": csv_info.max_significant_digits,
            "integer_literals": csv_info.integer_literals,
        },
        "residual_columns": {
            "present": residual_gz is not None,
            "columns": residual_cols,
            "sha256": sha256_bytes(residual_gz) if residual_gz else None,
            "content": "exact original text of the time and extra columns, and all comment lines",
        },
    }
    return CanonicalArtifact(
        mseed_bytes=mseed,
        mseed_sha256=sha256_bytes(mseed),
        encoding=enc,
        sidecar=sidecar,
        residual_gz=residual_gz,
        residual_sha256=sha256_bytes(residual_gz) if residual_gz else None,
        verification=verification,
    )


def verify_canonical(mseed: bytes, rec: Recording, codes: dict[str, tuple[str, str, str, str]]) -> dict:
    checks = []
    mapping = {c: ".".join(codes[c]) for c in ("N", "E", "Z")}
    try:
        back = read_miniseed([("canonical.mseed", mseed)], mapping=mapping)
    except InputError as exc:
        return {"ok": False, "checks": [{"name": "readback", "ok": False, "detail": exc.message}]}
    checks.append({"name": "readback", "ok": True, "detail": "read with the production MiniSEED reader"})
    checks.append({"name": "sampling_rate_exact", "ok": back.sampling_rate_hz == rec.sampling_rate_hz, "detail": f"{back.sampling_rate_hz!r} vs {rec.sampling_rate_hz!r}"})
    same_layout = len(back.segments) == len(rec.segments) and all(
        a.start_index == b.start_index and a.n == b.n for a, b in zip(back.segments, rec.segments, strict=False)
    )
    checks.append({"name": "segments", "ok": same_layout, "detail": f"{len(back.segments)} vs {len(rec.segments)} segments"})
    bitwise = same_layout and all(
        np.array_equal(np.ascontiguousarray(a.data, dtype=np.float64).view(np.uint64), np.ascontiguousarray(b.data, dtype=np.float64).view(np.uint64))
        for a, b in zip(back.segments, rec.segments, strict=False)
    )
    checks.append({"name": "samples_bitwise_binary64", "ok": bitwise, "detail": "sample arrays compared bit-for-bit as binary64"})
    if rec.t0_utc_ns is not None:
        expected_us = round(rec.t0_utc_ns / 1000) * 1000
        checks.append({"name": "start_time_microseconds", "ok": back.t0_utc_ns == expected_us, "detail": f"{back.t0_utc_ns} vs {expected_us}"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def extract_residual_columns(csv_bytes: bytes, info: CSVParseInfo) -> tuple[bytes | None, list[str]]:
    """Exact text of the time column, extra columns, and comment lines (gzip, deterministic)."""
    cols = ([info.time_column] if info.time_column else []) + list(info.extra_columns)
    if not cols and not info.comment_lines:
        return None, []
    raw = gzip.decompress(csv_bytes) if is_gzip(csv_bytes) else csv_bytes
    text = raw.decode("utf-8-sig")
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and (lines[idx].startswith("#") or not lines[idx].strip()):
        idx += 1
    header = [h.strip() for h in next(csv.reader([lines[idx]], delimiter=info.delimiter))]
    pos = [header.index(c) for c in cols]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for c in info.comment_lines:
        w.writerow(["#comment", c])
    w.writerow(["#columns", *cols])
    for row in csv.reader(lines[idx + 1 :], delimiter=info.delimiter):
        if not row or row[0].startswith("#") or (len(row) == 1 and not row[0].strip()):
            continue
        w.writerow([row[p] for p in pos])
    return gzip.compress(out.getvalue().encode("utf-8"), compresslevel=9, mtime=0), cols


def canonical_only_eligibility(rec: Recording, info: CSVParseInfo | None, artifact: CanonicalArtifact | None) -> tuple[bool, list[str]]:
    reasons = []
    if info is None:
        reasons.append("canonical-only applies to CSV sources only; MiniSEED originals are kept verbatim")
    if rec.diagnostics.errors:
        reasons.append("recording has validation errors")
    if rec.diagnostics.timing_irregular:
        reasons.append("irregular timing cannot be represented by the canonical format")
    if info is not None and info.max_significant_digits > 17:
        reasons.append("component values carry more significant digits than binary64 preserves")
    if artifact is None or not artifact.verification.get("ok"):
        reasons.append("canonical verification did not pass")
    if info is not None and artifact is not None and (info.time_column or info.extra_columns or info.comment_lines):
        if artifact.residual_gz is None:
            reasons.append("time/extra columns or comments could not be retained")
    return (not reasons), reasons


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


def finalize_canonical_only(store: ObjectStore, staged_original_key: str, artifact: CanonicalArtifact, prefix: str, consent: bool) -> dict:
    """Persist canonical artifacts, verify them FROM THE STORE, and only then delete the staged CSV.

    Any failure leaves the staged original untouched and returns ok=False.
    """
    if not consent:
        return {"ok": False, "original_deleted": False, "reason": "explicit canonical-only consent missing"}
    keys = {"mseed": f"{prefix}/canonical.mseed", "sidecar": f"{prefix}/canonical.sidecar.json"}
    if artifact.residual_gz is not None:
        keys["residual"] = f"{prefix}/canonical.residual.csv.gz"
    try:
        store.put(keys["mseed"], artifact.mseed_bytes)
        store.put(keys["sidecar"], artifact.sidecar_bytes)
        if "residual" in keys:
            store.put(keys["residual"], artifact.residual_gz)  # type: ignore[arg-type]
        if sha256_bytes(store.get(keys["mseed"])) != artifact.mseed_sha256:
            raise RuntimeError("stored canonical MiniSEED checksum mismatch")
        if "residual" in keys and sha256_bytes(store.get(keys["residual"])) != artifact.residual_sha256:
            raise RuntimeError("stored residual columns checksum mismatch")
        if store.get(keys["sidecar"]) != artifact.sidecar_bytes:
            raise RuntimeError("stored sidecar mismatch")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "original_deleted": False, "reason": f"canonical persistence failed: {exc}", "keys": keys}
    store.delete(staged_original_key)
    return {"ok": True, "original_deleted": True, "keys": keys, "original_bytes_retained": False}


def reconstructed_csv(mseed: bytes, sidecar: dict, residual_gz: bytes | None) -> bytes:
    """'Reconstructed CSV' from canonical-only storage. Never labeled as the original file."""
    mapping = {c["component"]: c["miniseed_id"] for c in sidecar["channels"]}
    rec = read_miniseed([("canonical.mseed", mseed)], mapping=mapping)
    csv_meta = sidecar["csv"] or {}
    header = csv_meta.get("header") or ["N", "E", "Z"]
    comp_cols = csv_meta.get("component_columns") or {"N": "N", "E": "E", "Z": "Z"}
    residual: dict[str, list[str]] = {}
    comments: list[str] = []
    if residual_gz:
        rows = list(csv.reader(io.StringIO(gzip.decompress(residual_gz).decode("utf-8"))))
        cols: list[str] = []
        body = []
        for row in rows:
            if row and row[0] == "#comment":
                comments.append(row[1])
            elif row and row[0] == "#columns":
                cols = row[1:]
            else:
                body.append(row)
        for i, c in enumerate(cols):
            residual[c] = [r[i] for r in body]
    data = np.concatenate([s.data for s in rec.segments], axis=1)
    is_int = sidecar.get("sample_dtype") == "int32"
    out = io.StringIO()
    out.write("# Reconstructed CSV generated from the canonical recording; original CSV bytes were not retained.\n")
    for c in comments:
        out.write(c + "\n")
    delim = csv_meta.get("delimiter", ",")
    out.write(delim.join(header) + "\n")
    comp_of = {v: k for k, v in comp_cols.items()}
    ci = {"N": 0, "E": 1, "Z": 2}
    for i in range(data.shape[1]):
        fields = []
        for h in header:
            if h in comp_of:
                v = data[ci[comp_of[h]], i]
                fields.append(str(int(v)) if is_int else repr(float(v)))
            else:
                fields.append(residual.get(h, [""] * data.shape[1])[i])
        out.write(delim.join(fields) + "\n")
    return out.getvalue().encode("utf-8")
