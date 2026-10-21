"""Submission pipeline: validate, reserve quota, stage, commit immutable objects, then one DB commit.

Order of durability (spec 3.2, 4): nothing is published until every committed object was written
and read back with a matching checksum and the measurement rows, metadata snapshot, artifacts, and
quota charge commit in a single database transaction. Staged bytes are removed only afterwards.
On any failure the staged objects are discarded and the reservation is released; committed,
content-addressed objects are never deleted by this path because other measurements may share them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hvsr_core.io import CSVOptions, build_canonical, preserve_original, read_csv, read_miniseed
from hvsr_core.io.csv_reader import Layout
from hvsr_core.io.preservation import canonical_only_eligibility, detect_format, finalize_canonical_only
from hvsr_core.io.stationxml import apply_stationxml, parse_stationxml
from hvsr_core.jsonutil import canonical_dumps, sha256_bytes
from hvsr_core.metadata import MeasurementMetadata
from hvsr_core.model import InputError, ns_to_iso
from hvsr_core.versions import PACKAGE_VERSION

from . import quota
from .context import AppContext
from .db import Artifact, Contributor, Measurement, MetadataRevision, StagedUpload, utc_iso
from .licensing import ConsentForm, licensing_record
from .photos import process_photo
from .records import allocate_record_code, identity_for
from .storage import blob_key

Role = Literal["waveform", "stationxml", "photo_sensor", "photo_area"]
MIME = {
    "csv": "text/csv",
    "csv.gz": "application/gzip",
    "miniseed": "application/vnd.fdsn.mseed",
    "miniseed3": "application/vnd.fdsn.mseed",
    "stationxml": "application/xml",
    "json": "application/json",
    "jpeg": "image/jpeg",
}
ORIGINAL_LABEL = {
    "csv": "Original CSV",
    "csv.gz": "Original CSV.GZ",
    "miniseed": "Original MiniSEED",
    "miniseed3": "Original MiniSEED 3",
}


@dataclass(frozen=True)
class UploadFile:
    filename: str
    data: bytes = field(repr=False)
    role: Role = "waveform"


class WaveformOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    csv_layout: Layout | None = None
    csv_sampling_rate_hz: float | None = Field(None, gt=0)
    csv_start_time_utc: str | None = None
    csv_timestamps_declared_utc: bool = False
    csv_column_map: dict[str, str] | None = None
    csv_time_column: str | None = None
    miniseed_mapping: dict[str, str] | None = None
    canonical_only: bool = Field(
        False,
        description="Opt-in: keep verified canonical MiniSEED + sidecars instead of the original CSV bytes.",
    )


class SubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    metadata: MeasurementMetadata
    consent: ConsentForm
    waveform: WaveformOptions = Field(default_factory=WaveformOptions)


@dataclass
class SubmissionResult:
    measurement_id: str
    record_code: str
    existing: bool
    newly_charged: bool
    charged_logical_bytes: int
    stored_bytes: int
    transfer_bytes: int
    notes: list[str]


class LimitExceeded(InputError):
    pass


def _art_id() -> str:
    return "art_" + uuid.uuid4().hex[:24]


def parse_waveforms(files: list[UploadFile], opts: WaveformOptions, limits):
    """Parse the waveform files; any reader ceiling (`*_limit` codes) surfaces as LimitExceeded."""
    try:
        return _parse_waveforms(files, opts, limits)
    except InputError as exc:
        if exc.code.endswith("_limit") and not isinstance(exc, LimitExceeded):
            raise LimitExceeded(exc.code, exc.message) from exc
        raise


def _parse_waveforms(files: list[UploadFile], opts: WaveformOptions, limits):
    wav = [f for f in files if f.role == "waveform"]
    if not wav:
        raise InputError("no_waveform", "at least one waveform file is required")
    fmts = [detect_format(f.filename, f.data) for f in wav]
    csv_info = csv_bytes = None
    if any(f in ("csv", "csv.gz") for f in fmts):
        if len(wav) != 1:
            raise InputError("csv_files", "a CSV submission must be a single file with all three components")
        if opts.csv_layout is None:
            raise InputError(
                "csv_layout",
                "the CSV layout must be declared (elapsed_time, utc_timestamps, samples_with_rate)",
            )
        csv_opts = CSVOptions(
            layout=opts.csv_layout,
            column_map=opts.csv_column_map,
            sampling_rate_hz=opts.csv_sampling_rate_hz,
            start_time_utc=opts.csv_start_time_utc,
            timestamps_declared_utc=opts.csv_timestamps_declared_utc,
            time_column=opts.csv_time_column,
        )
        rec, csv_info = read_csv(wav[0].data, csv_opts, limits, filename=wav[0].filename)
        csv_bytes = wav[0].data
    else:
        rec = read_miniseed([(f.filename, f.data) for f in wav], mapping=opts.miniseed_mapping, limits=limits)
    if rec.span_samples > limits.max_samples_per_channel:
        raise LimitExceeded(
            "sample_limit",
            f"{rec.span_samples} samples per channel exceed the limit {limits.max_samples_per_channel}",
        )
    duration = rec.span_samples / rec.sampling_rate_hz
    if duration > limits.max_duration_s:
        raise LimitExceeded(
            "duration_limit", f"duration {duration:.0f} s exceeds the limit {limits.max_duration_s:.0f} s"
        )
    return rec, csv_info, csv_bytes, fmts


def _summary(rec) -> dict:
    d = rec.diagnostics
    return {
        "sampling_rate_hz": rec.sampling_rate_hz,
        "start_utc": ns_to_iso(rec.t0_utc_ns) if rec.t0_utc_ns is not None else None,
        "samples_per_channel": int(rec.span_samples),
        "duration_s": rec.span_samples / rec.sampling_rate_hz,
        "channels": [vars(c) for c in rec.channels],
        "segments": len(rec.segments),
        "gaps": len(d.gaps),
        "timing_irregular": bool(d.timing_irregular),
        "clipped_samples": dict(d.clipped_samples),
        "warnings": list(d.warnings),
        "sample_dtype": rec.sample_dtype,
    }


def decoded_waveform_bytes(rec) -> int:
    """Decoded N/E/Z samples at their native width (int32: 4 bytes, float64: 8 bytes)."""
    return 3 * rec.total_samples * (4 if rec.sample_dtype == "int32" else 8)


def _put_verified(ctx: AppContext, key: str, data: bytes, mime: str, written: dict[str, int]) -> None:
    ctx.store.put(key, data, mime)
    if sha256_bytes(ctx.store.get(key)) != sha256_bytes(data):
        raise RuntimeError(f"stored object {key} failed read-back verification")
    written[key] = len(data)


def submit(
    ctx: AppContext, contributor: Contributor, req: SubmissionRequest, files: list[UploadFile]
) -> SubmissionResult:
    s = ctx.settings
    now = ctx.clock()
    stamp = utc_iso(now)
    with ctx.sessions() as session:
        done = session.scalar(
            select(Measurement).where(
                Measurement.contributor_id == contributor.id,
                Measurement.submission_key == req.idempotency_key,
            )
        )
        if done is not None:
            return SubmissionResult(
                done.id, done.record_code, True, False, 0, 0, 0, ["idempotent replay: already accepted"]
            )

    lic = licensing_record(req.consent, stamp)  # raises ConsentRequired before any work
    transfer = sum(len(f.data) for f in files)
    if transfer > s.max_request_bytes:
        raise LimitExceeded(
            "request_size_limit", f"request of {transfer} bytes exceeds {s.max_request_bytes}"
        )
    if len(files) > s.max_files:
        raise LimitExceeded("file_limit", f"{len(files)} files exceed the limit of {s.max_files}")
    limits = s.input_limits()
    rec, csv_info, csv_bytes, fmts = parse_waveforms(files, req.waveform, limits)

    notes: list[str] = []
    response_info = None
    sx = [f for f in files if f.role == "stationxml"]
    if len(sx) > 1:
        raise InputError("stationxml", "at most one StationXML file")
    if sx:
        inv = parse_stationxml(sx[0].data)
        rec, response_info = apply_stationxml(rec, inv, sha256_bytes(sx[0].data))
    photos = [(f, *process_photo(f.data)) for f in files if f.role.startswith("photo_")]
    originals = [(f, preserve_original(f.filename, f.data, limits)) for f in files if f.role == "waveform"]
    logical = sum(o.logical_uncompressed_size for _, o in originals)
    if csv_info is None:  # MiniSEED: Steim compression must not be a quota bypass (docs/quotas.md)
        logical = max(logical, decoded_waveform_bytes(rec))
    logical += sum(len(f.data) for f in sx) + sum(len(f.data) for f, *_ in photos)

    # canonical numerical recording: stored only under canonical-only; otherwise it is regenerated
    # deterministically from the preserved original on request (no second copy of the samples)
    canonical = None
    canonical_only = False
    if csv_info is not None and req.waveform.canonical_only:
        try:
            canonical = build_canonical(rec, originals[0][1], csv_info, csv_bytes)
        except InputError as exc:
            notes.append(f"canonical recording not produced: {exc}")
        ok, reasons = canonical_only_eligibility(rec, csv_info, canonical)
        canonical_only = ok
        if not ok:
            notes.append(
                "canonical-only refused; the compressed original CSV is preserved: " + "; ".join(reasons)
            )
    elif req.waveform.canonical_only:
        notes.append("canonical-only applies to CSV sources only; the MiniSEED original is kept verbatim")

    # 1. reserve quota (own transaction so concurrent submissions see it)
    with ctx.sessions() as session, session.begin():
        res = quota.reserve(session, contributor, s, req.idempotency_key, logical, now)
        entry_id, newly = res.entry.id, res.newly_charged

    sub_id = uuid.uuid4().hex
    staged_keys: list[str] = []
    measurement_id = str(uuid.uuid4())
    try:
        # 2. stage raw uploads
        with ctx.sessions() as session, session.begin():
            for i, f in enumerate(files):
                key = f"staging/{sub_id}/{i:02d}"
                ctx.store.put(key, f.data)
                staged_keys.append(key)
                session.add(
                    StagedUpload(
                        submission_key=req.idempotency_key,
                        contributor_id=contributor.id,
                        object_key=key,
                        state="staged",
                        created_at=now.timestamp(),
                    )
                )

        # 3. committed immutable objects
        arts: list[Artifact] = []
        written: dict[str, int] = {}

        def art(
            category,
            kind,
            label,
            mime,
            data=None,
            *,
            key=None,
            size=None,
            sha=None,
            stored_sha=None,
            enc=None,
            avail="available",
            reason=None,
            refs=(),
            filename=None,
            obj_cat="metadata",
        ):
            if data is not None:
                sha = sha or sha256_bytes(data)
                key = key or blob_key(obj_cat, sha256_bytes(data))
                _put_verified(ctx, key, data, mime, written)
                size = len(data) if size is None else size
                stored_sha = stored_sha or sha256_bytes(data)
            a = Artifact(
                id=_art_id(),
                measurement_id=measurement_id,
                run_id=None,
                category=category,
                kind=kind,
                label=label,
                filename=filename,
                blob_key=key,
                mime_type=mime,
                byte_size=size,
                sha256=sha,
                stored_sha256=stored_sha,
                stored_encoding=enc,
                availability=avail,
                availability_reason=reason,
                generated_by={"software": "hvsr-analyzer", "version": PACKAGE_VERSION, "stage": "submission"},
                source_refs=list(refs),
                method=None,
                created_utc=stamp,
            )
            arts.append(a)
            return a

        orig_arts = []
        for (f, o), fmt in zip(originals, fmts, strict=True):
            if canonical_only and fmt in ("csv", "csv.gz"):
                orig_arts.append(
                    art(
                        "original_data",
                        "original_" + fmt.replace(".", "_"),
                        ORIGINAL_LABEL[fmt] + " (not retained)",
                        MIME[fmt],
                        size=o.original_size,
                        sha=o.original_sha256,
                        avail="unavailable",
                        filename=f.filename,
                        reason="Original CSV bytes were not retained (explicit canonical-only policy). The SHA-256 is recorded for reference only; it does not substitute for the discarded file.",
                    )
                )
                continue
            key = blob_key("recordings", o.stored_sha256)
            _put_verified(
                ctx,
                key,
                o.stored_bytes,
                MIME["csv.gz"] if o.stored_encoding == "gzip" else MIME[fmt],
                written,
            )
            orig_arts.append(
                art(
                    "original_data",
                    "original_" + fmt.replace(".", "_"),
                    ORIGINAL_LABEL[fmt],
                    MIME[fmt],
                    key=key,
                    size=o.original_size,
                    sha=o.original_sha256,
                    stored_sha=o.stored_sha256,
                    enc=o.stored_encoding,
                    filename=f.filename,
                )
            )
        refs = [a.id for a in orig_arts]
        if canonical is not None and canonical_only:
            staged_csv = staged_keys[next(i for i, f in enumerate(files) if f.role == "waveform")]
            out = finalize_canonical_only(
                ctx.store, staged_csv, canonical, f"recordings/measurements/{measurement_id}", consent=True
            )
            if not out["ok"]:
                raise RuntimeError(out["reason"])
            k = out["keys"]
            written[k["mseed"]] = len(canonical.mseed_bytes)
            written[k["sidecar"]] = len(canonical.sidecar_bytes)
            if "residual" in k:
                written[k["residual"]] = len(canonical.residual_gz)
            art(
                "canonical_recordings",
                "canonical_miniseed",
                "Canonical recording",
                MIME["miniseed"],
                key=k["mseed"],
                size=len(canonical.mseed_bytes),
                sha=canonical.mseed_sha256,
                stored_sha=canonical.mseed_sha256,
                enc="identity",
                refs=refs,
            )
            art(
                "canonical_recordings",
                "canonical_sidecar",
                "Canonical recording sidecar",
                MIME["json"],
                key=k["sidecar"],
                size=len(canonical.sidecar_bytes),
                sha=sha256_bytes(canonical.sidecar_bytes),
                stored_sha=sha256_bytes(canonical.sidecar_bytes),
                enc="identity",
                refs=refs,
            )
            if "residual" in k:
                art(
                    "canonical_recordings",
                    "canonical_residual_columns",
                    "Retained time/extra CSV columns",
                    MIME["csv.gz"],
                    key=k["residual"],
                    size=len(canonical.residual_gz),
                    sha=canonical.residual_sha256,
                    stored_sha=canonical.residual_sha256,
                    enc="identity",
                    refs=refs,
                )
            art(
                "canonical_recordings",
                "reconstructed_csv",
                "Reconstructed CSV",
                MIME["csv"],
                avail="on_demand",
                reason="generated from the canonical recording on request; not the original file",
                refs=refs,
            )
        elif csv_info is not None:
            art(
                "canonical_recordings",
                "canonical_miniseed",
                "Canonical recording",
                MIME["miniseed"],
                avail="on_demand",
                reason="generated deterministically from the preserved original CSV on request",
                refs=refs,
            )
        else:
            for a in orig_arts:
                if a.kind.startswith("original_miniseed"):
                    art(
                        "canonical_recordings",
                        "canonical_miniseed",
                        "Canonical recording (the original MiniSEED)",
                        a.mime_type,
                        key=a.blob_key,
                        size=a.byte_size,
                        sha=a.sha256,
                        stored_sha=a.stored_sha256,
                        enc="identity",
                        refs=[a.id],
                    )
        art(
            "canonical_recordings",
            "normalized_csv",
            "Normalized waveform CSV",
            MIME["csv"],
            avail="on_demand",
            reason="generated from the stored recording on request",
            refs=refs,
        )

        meta_doc = req.metadata.model_dump(mode="json")
        art(
            "sensor_metadata",
            "measurement_metadata",
            "Measurement metadata (revision 1)",
            MIME["json"],
            canonical_dumps(meta_doc).encode(),
        )
        for f in sx:
            art(
                "sensor_metadata",
                "original_stationxml",
                "Original StationXML",
                MIME["stationxml"],
                f.data,
                filename=f.filename,
                obj_cat="metadata",
            )
        for f, data, _info in photos:
            label = "Sensor photograph" if f.role == "photo_sensor" else "Area photograph"
            art(
                "sensor_metadata",
                f.role,
                label + " (300 x 300, EXIF removed)",
                MIME["jpeg"],
                data,
                obj_cat="photos",
            )
        public_lic = {k: v for k, v in lic.items()}
        art(
            "licensing",
            "license_record",
            "License, consent, and attribution",
            MIME["json"],
            canonical_dumps(public_lic).encode(),
        )
        prov = {
            "submitted_utc": stamp,
            "software_version": PACKAGE_VERSION,
            "parse_options": req.waveform.model_dump(mode="json"),
            "originals": [o.describe() for _, o in originals],
            "canonical": ("on_demand" if csv_info is not None else None)
            if canonical is None
            else {
                "encoding": canonical.encoding,
                "sha256": canonical.mseed_sha256,
                "verification": canonical.verification,
            },
            "canonical_only": canonical_only,
            "response": response_info,
            "notes": notes,
        }
        art(
            "provenance",
            "submission_provenance",
            "Submission provenance",
            MIME["json"],
            canonical_dumps(prov).encode(),
        )
        stored = sum(written.values())

        # 4. one transaction publishes everything
        ident = identity_for(req.metadata.latitude, req.metadata.longitude, rec.t0_utc_ns, now)
        with ctx.sessions() as session, session.begin():
            code, n = allocate_record_code(session, ident["base_code"])
            session.add(
                Measurement(
                    id=measurement_id,
                    record_code=code,
                    base_code=ident["base_code"],
                    suffix_n=n,
                    geohash=ident["geohash"],
                    decimal_year=ident["decimal_year"],
                    decimal_year_source=ident["decimal_year_source"],
                    created_utc=stamp,
                    contributor_id=contributor.id,
                    submission_key=req.idempotency_key,
                    moderation_status="public",
                    original_bytes_retained=not canonical_only,
                    original_bytes_note=None
                    if not canonical_only
                    else "Original CSV bytes were not retained by explicit canonical-only consent; downloads are the Canonical recording and a Reconstructed CSV.",
                    current_revision=1,
                    site_group_id=req.metadata.site_group_id,
                    licensing=lic,
                    parse_options=req.waveform.model_dump(mode="json"),
                    recording_summary=_summary(rec),
                )
            )
            session.flush()
            session.add(
                MetadataRevision(
                    measurement_id=measurement_id,
                    revision=1,
                    metadata_doc=meta_doc,
                    created_utc=stamp,
                    reason="initial submission",
                    author_attribution=lic["attribution"],
                )
            )
            session.add_all(arts)
            entry = session.get(quota.QuotaEntry, entry_id)
            quota.commit(session, entry, stored, transfer, now)
            for su in session.scalars(select(StagedUpload).where(StagedUpload.object_key.in_(staged_keys))):
                su.state = "committed"
    except IntegrityError:
        _discard(ctx, staged_keys, entry_id, newly, now, "duplicate submission key")
        with ctx.sessions() as session:
            done = session.scalar(
                select(Measurement).where(
                    Measurement.contributor_id == contributor.id,
                    Measurement.submission_key == req.idempotency_key,
                )
            )
        if done is None:
            raise
        return SubmissionResult(
            done.id, done.record_code, True, False, 0, 0, 0, ["idempotent replay: already accepted"]
        )
    except BaseException as exc:
        _discard(ctx, staged_keys, entry_id, newly, now, f"{type(exc).__name__}: {exc}")
        raise
    _clean_staging(ctx, staged_keys)
    return SubmissionResult(measurement_id, code, False, newly, logical, stored, transfer, notes)


def _discard(ctx: AppContext, keys: list[str], entry_id: str, newly: bool, now, note: str) -> None:
    for k in keys:
        ctx.store.delete(k)
    with ctx.sessions() as session, session.begin():
        for su in session.scalars(select(StagedUpload).where(StagedUpload.object_key.in_(keys))):
            su.state = "discarded"
        if newly:
            entry = session.get(quota.QuotaEntry, entry_id)
            quota.release(session, entry, now, note)


def _clean_staging(ctx: AppContext, keys: list[str]) -> None:
    for k in keys:
        try:
            ctx.store.delete(k)
        except Exception:  # noqa: BLE001,S112 - the sweeper retries later
            continue
    with ctx.sessions() as session, session.begin():
        for su in session.scalars(select(StagedUpload).where(StagedUpload.object_key.in_(keys))):
            if not ctx.store.exists(su.object_key):
                su.state = "cleaned"


def sweep_staging(ctx: AppContext) -> dict:
    """Delete staged objects that are committed-and-copied, discarded, or older than the staging TTL,
    and release reservations left behind by a crashed submission (never one that was accepted).

    Only keys under `staging/` are ever touched; committed objects are out of scope by construction.
    """
    out = _sweep_objects(ctx)
    out["released_reservations"] = _release_stale_reservations(ctx)
    return out


def _release_stale_reservations(ctx: AppContext) -> list[str]:
    now = ctx.clock()
    cutoff = now.timestamp() - ctx.settings.staging_ttl_s
    released = []
    with ctx.sessions() as session, session.begin():
        for entry in session.scalars(
            select(quota.QuotaEntry).where(quota.QuotaEntry.state == "reserved")
        ).all():
            touched = dt.datetime.strptime(entry.updated_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            if touched.timestamp() >= cutoff:
                continue
            accepted = session.scalar(
                select(Measurement.id).where(
                    Measurement.contributor_id == entry.contributor_id,
                    Measurement.submission_key == entry.idempotency_key,
                )
            )
            if accepted is None:
                quota.release(session, entry, now, "stale reservation released by the staging sweeper")
                released.append(entry.id)
    return released


def _sweep_objects(ctx: AppContext) -> dict:
    now = ctx.clock().timestamp()
    cutoff = now - ctx.settings.staging_ttl_s
    removed = []
    with ctx.sessions() as session, session.begin():
        rows = {su.object_key: su for su in session.scalars(select(StagedUpload))}
        for info in ctx.store.list("staging/"):
            su = rows.get(info.key)
            stale = su is None and info.modified_epoch_s < cutoff
            if stale or (
                su is not None
                and (su.state in ("committed", "discarded", "cleaned") or su.created_at < cutoff)
            ):
                ctx.store.delete(info.key)
                removed.append(info.key)
                if su is not None and su.state != "cleaned":
                    su.state = "cleaned" if su.state == "committed" else "discarded"
    return {"removed": removed}
