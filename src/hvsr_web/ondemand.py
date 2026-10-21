"""Artifacts generated on request (spec 13.2): canonical and reconstructed recordings, the normalized
waveform CSV.GZ, and per-window spectra.

The artifact row exists from the start with availability "on_demand" and a reason. Requesting it
enqueues ONE durable job per artifact (idempotency key "generate:<artifact id>"). The job regenerates
the bytes deterministically from stored inputs, stores them content-addressed, and turns the row
"available" in a fenced transaction together with its compute-usage record; afterwards the artifact
is served from storage like any other. Inputs that no longer reproduce the recorded hashes are a
permanent failure, never a silent substitute.
"""

from __future__ import annotations

import time

from sqlalchemy import select, update

from hvsr_core import exports
from hvsr_core.analysis import science_key
from hvsr_core.config import AnalysisRequest
from hvsr_core.core import run_core
from hvsr_core.io.preservation import (
    build_canonical,
    preserve_original,
    reconstruct_original,
    reconstructed_csv,
)
from hvsr_core.io.stationxml import apply_stationxml, parse_stationxml
from hvsr_core.jsonutil import sha256_bytes, strict_loads
from hvsr_core.metadata import MeasurementMetadata, apply_sensor_metadata
from hvsr_core.strings import t
from hvsr_core.versions import PACKAGE_VERSION

from . import jobs
from .analysis_jobs import analysis_inputs, load_recording
from .context import AppContext
from .db import AnalysisRun, Artifact, ComputeUsage, Job, Measurement, MetadataRevision, utc_iso
from .publication import artifact_id, store_verified
from .quota import period_of
from .submissions import UploadFile, WaveformOptions, parse_waveforms

KIND = "generate"


class GenerationRefused(jobs.PermanentJobError):
    pass


def request_generation(ctx: AppContext, a: Artifact) -> Job:
    return jobs.enqueue(ctx, KIND, {"artifact_id": a.id}, f"{KIND}:{a.id}")


def generation_job(ctx: AppContext, artifact_id_: str) -> Job | None:
    with ctx.sessions() as session:
        return session.scalar(select(Job).where(Job.idempotency_key == f"{KIND}:{artifact_id_}"))


# ------------------------------------------------------------------------------------- generators
def _verified(ctx, a: Artifact) -> bytes:
    data = ctx.store.get(a.blob_key)
    if sha256_bytes(data) != a.stored_sha256:
        raise RuntimeError(f"{a.blob_key} failed its SHA-256 check")
    return data


def _by_kind(arts, category):
    return {a.kind: a for a in arts if a.category == category and a.availability == "available"}


def gen_normalized(ctx, m, a, arts, session_data):
    snap = session_data["current_snapshot"]
    rec, sources, _ = load_recording(ctx, m, arts)
    rec = apply_sensor_metadata(rec, MeasurementMetadata.model_validate(snap.metadata_doc).sensor)
    info = {
        "table": "normalized_waveform",
        "metadata_revision": snap.revision,
        "source_refs": [s.id for s in sources],
    }
    return exports.normalized_waveform_csv_gz(rec), "recordings", info, []


def gen_canonical(ctx, m, a, arts, session_data):
    originals = sorted(
        (x for x in arts if x.category == "original_data" and x.availability == "available"),
        key=lambda x: (x.filename or "", x.sha256 or ""),
    )
    if not originals:
        raise GenerationRefused(f"{m.record_code}: no preserved original to convert")
    files = [
        UploadFile(
            x.filename or x.kind, reconstruct_original(ctx.store.get(x.blob_key), x.stored_encoding, x.sha256)
        )
        for x in originals
    ]
    limits = ctx.settings.input_limits()
    rec, csv_info, csv_bytes, _ = parse_waveforms(
        files, WaveformOptions.model_validate(m.parse_options), limits
    )
    if csv_info is None:
        raise GenerationRefused(f"{m.record_code}: the canonical recording is the original MiniSEED")
    sx = [x for x in arts if x.kind == "original_stationxml"]
    if sx:
        data = _verified(ctx, sx[0])
        rec, _ = apply_stationxml(rec, parse_stationxml(data), sha256_bytes(data))
    can = build_canonical(rec, preserve_original(files[0].filename, csv_bytes, limits), csv_info, csv_bytes)
    side = [("canonical_sidecar", t("artifact.canonical_sidecar"), can.sidecar_bytes, "application/json")]
    if can.residual_gz is not None:
        side.append(
            (
                "canonical_residual_columns",
                t("artifact.residual_columns"),
                can.residual_gz,
                "application/gzip",
            )
        )
    info = {
        "encoding": can.encoding,
        "verification": can.verification,
        "source_refs": [x.id for x in originals],
    }
    return can.mseed_bytes, "recordings", info, side


def gen_reconstructed(ctx, m, a, arts, session_data):
    canon = _by_kind(arts, "canonical_recordings")
    mseed_art, side_art = canon.get("canonical_miniseed"), canon.get("canonical_sidecar")
    if mseed_art is None or side_art is None:
        raise GenerationRefused(f"{m.record_code}: the canonical recording and sidecar are required")
    residual = canon.get("canonical_residual_columns")
    data = reconstructed_csv(
        _verified(ctx, mseed_art),
        strict_loads(_verified(ctx, side_art)),
        _verified(ctx, residual) if residual else None,
    )
    refs = [x.id for x in (mseed_art, side_art, residual) if x is not None]
    return data, "recordings", {"source_refs": refs}, []


def gen_window_spectra(ctx, m, a, arts, session_data):
    run, snap = session_data["run"], session_data["run_snapshot"]
    config = AnalysisRequest.model_validate(run.request).config
    rec, sources, info = analysis_inputs(ctx, m, arts, snap, config)
    if science_key(rec, config, info) != run.science_key:
        raise GenerationRefused(
            f"the stored inputs no longer reproduce the science key of run {run.run_id}; "
            "per-window spectra cannot be regenerated for it"
        )
    core = run_core(rec, config, info.instrument, info.measurement_context)
    cells = int(core.spectra.window.count) * int(core.spectra.grid.valid.sum())
    if cells > ctx.settings.max_window_spectra_cells:
        raise GenerationRefused(
            t(
                "artifact.reason.window_spectra_too_large",
                cells=cells,
                limit=ctx.settings.max_window_spectra_cells,
            )
        )
    gen = {"table": "window_spectra", "science_key": run.science_key, "source_refs": [s.id for s in sources]}
    return exports.window_spectra_csv_gz(core), "analyses", gen, []


GENERATORS = {
    "normalized_csv": gen_normalized,
    "canonical_miniseed": gen_canonical,
    "reconstructed_csv": gen_reconstructed,
    "window_spectra": gen_window_spectra,
}


# ------------------------------------------------------------------------------------------ handler
def handle_generate(ctx: AppContext, claim: jobs.Claim) -> dict:
    t_wall, t_cpu = time.perf_counter(), time.process_time()
    art_id = claim.payload["artifact_id"]
    with ctx.sessions() as session:
        a = session.get(Artifact, art_id)
        m = session.get(Measurement, a.measurement_id) if a else None
        if a is None or m is None:
            raise GenerationRefused(f"no such artifact {art_id}")
        if a.availability == "available":
            return {"artifact_id": a.id, "sha256": a.sha256, "already_available": True}
        if a.availability != "on_demand" or m.moderation_status == "removed":
            raise GenerationRefused(f"artifact {a.id} is {a.availability}; it is not generated on request")
        if a.kind not in GENERATORS:
            raise GenerationRefused(f"no generator for {a.kind}")
        arts = list(
            session.scalars(
                select(Artifact).where(Artifact.measurement_id == m.id, Artifact.run_id.is_(None))
            )
        )
        data_ctx = {"current_snapshot": session.get(MetadataRevision, (m.id, m.current_revision))}
        if a.run_id:
            run = session.get(AnalysisRun, a.run_id)
            data_ctx["run"] = run
            data_ctx["run_snapshot"] = session.get(MetadataRevision, (m.id, run.metadata_revision))
    data, obj_category, info, side = GENERATORS[a.kind](ctx, m, a, arts, data_ctx)
    refs = info.pop("source_refs", list(a.source_refs))
    if not jobs.heartbeat(ctx, claim):
        raise jobs.LeaseLost(claim.job_id)
    key = store_verified(ctx, obj_category, data, a.mime_type)
    sha = sha256_bytes(data)
    stamp = utc_iso(ctx.clock())
    generated = dict(
        a.generated_by or {}, generated_on_request=stamp, job_id=claim.job_id, version=PACKAGE_VERSION, **info
    )
    side_rows = [
        Artifact(
            id=artifact_id(a.id, kind, sha256_bytes(payload)),
            measurement_id=m.id,
            run_id=a.run_id,
            category=a.category,
            kind=kind,
            label=label,
            filename=None,
            blob_key=store_verified(ctx, obj_category, payload, mime),
            mime_type=mime,
            byte_size=len(payload),
            sha256=sha256_bytes(payload),
            stored_sha256=sha256_bytes(payload),
            stored_encoding="identity",
            availability="available",
            availability_reason=None,
            generated_by=dict(generated, companion_of=a.id),
            source_refs=refs,
            method=None,
            created_utc=stamp,
        )
        for kind, label, payload, mime in side
    ]
    result = {
        "artifact_id": a.id,
        "sha256": sha,
        "byte_size": len(data),
        "companions": [r.id for r in side_rows],
        "wall_s": time.perf_counter() - t_wall,
        "cpu_s": time.process_time() - t_cpu,
    }
    with ctx.sessions() as session, session.begin():
        session.execute(
            update(Artifact)
            .where(Artifact.id == a.id, Artifact.availability == "on_demand")
            .values(
                blob_key=key,
                byte_size=len(data),
                sha256=sha,
                stored_sha256=sha,
                stored_encoding="identity",
                availability="available",
                availability_reason=None,
                generated_by=generated,
                source_refs=refs,
            )
        )
        session.add_all([r for r in side_rows if session.get(Artifact, r.id) is None])
        session.add(
            ComputeUsage(
                contributor_id=None,
                period=period_of(ctx.clock()),
                kind=f"generate:{a.kind}",
                job_id=claim.job_id,
                estimate={"artifact_id": a.id, "kind": a.kind},
                cpu_s=result["cpu_s"],
                wall_s=result["wall_s"],
                created_utc=stamp,
            )
        )
        jobs.complete_in(session, claim, result, ctx.clock().timestamp())
    return {**result, jobs.COMPLETED: True}


HANDLERS = {KIND: handle_generate}
