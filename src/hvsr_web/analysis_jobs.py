"""Analysis jobs: compute-budget pre-check, durable enqueue, and idempotent, fenced publication.

A run's identity is its science key (recording content, scientific configuration, measurement
snapshot, algorithm and template-bank versions). Science documents are cached immutably in the
object store under that key, so a retry after a crash reuses them byte for byte. Publication
inserts run rows and artifact rows with deterministic IDs, records compute usage, and marks the
job succeeded in ONE fenced transaction: a worker whose lease was taken over cannot publish, and
publishing the same run again adds nothing.
"""

from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hvsr_core.analysis import RecordInfo, run_analysis
from hvsr_core.config import AnalysisRequest, robustness_config
from hvsr_core.io.preservation import reconstruct_original, recording_from_canonical
from hvsr_core.io.stationxml import apply_stationxml, parse_stationxml
from hvsr_core.jsonutil import canonical_dumps, canonical_hash, sha256_bytes, strict_loads
from hvsr_core.metadata import MeasurementMetadata, apply_sensor_metadata, evaluate_instrument
from hvsr_core.templates import resolve_sdof_bank
from hvsr_core.versions import PACKAGE_VERSION
from hvsr_core.windows import sample_counts

from . import jobs
from .context import AppContext
from .db import AnalysisRun, Artifact, ComputeUsage, Measurement, MetadataRevision, utc_iso
from .quota import period_of
from .storage import ImmutableObjectError, blob_key
from .submissions import UploadFile, WaveformOptions, parse_waveforms

ADVICE = (
    "Options: increase the window step, turn off the 1.5 x Lw robustness comparison, reduce the "
    "bootstrap replicates, process the recording offline with the command-line tool, or ask an "
    "administrator for a larger compute allowance. Records are never silently shortened to fit."
)
METHOD_LABEL = {"standard": "Standard H/V analysis (JSON)", "sdof": "SDOF-selected H/V analysis (JSON)"}


class ComputeBudgetExceeded(jobs.PermanentJobError):
    def __init__(self, problems: list[str], estimate: dict):
        super().__init__("; ".join(problems) + ". " + ADVICE)
        self.problems, self.estimate = problems, estimate


class MeasurementUnavailable(jobs.PermanentJobError):
    pass


# ------------------------------------------------------------------------------------ cost estimate
def _windows(duration_s: float, fs: float, cfg) -> int:
    n, step = sample_counts(fs, cfg.windowing.window_length_s, cfg.windowing.step_s)
    total = int(round(duration_s * fs))
    return 0 if total < n else (total - n) // step + 1


def estimate_cost(summary: dict, request: AnalysisRequest) -> dict:
    fs, duration = float(summary["sampling_rate_hz"]), float(summary["duration_s"])
    configs = [("A", request.config)]
    if request.robustness_check:
        try:
            configs.append(("B", robustness_config(request.config, fs)))
        except ValueError:
            pass  # run B is reported as not evaluable; it costs nothing
    runs = []
    for name, cfg in configs:
        bank, _ = resolve_sdof_bank(cfg.sdof.template_bank, cfg.spectral.ko_bandwidth)
        w = _windows(duration, fs, cfg)
        runs.append(
            {
                "run": name,
                "window_length_s": cfg.windowing.window_length_s,
                "step_s": cfg.windowing.step_s,
                "candidate_windows": w,
                "templates": bank.n_templates,
                "template_comparisons_per_family": w * bank.n_templates,
                "bootstrap_replicates": cfg.uncertainty.n_bootstrap,
            }
        )
    return {
        "runs": runs,
        "parameter_runs": len(runs),
        "max_candidate_windows": max(r["candidate_windows"] for r in runs),
        "template_comparisons_per_family": sum(r["template_comparisons_per_family"] for r in runs),
        "bootstrap_replicates": max(r["bootstrap_replicates"] for r in runs),
        "basis": "upper bounds from the stored recording summary (gaps only reduce them); template "
        "comparisons are counted per plausible peak family",
    }


def check_budget(estimate: dict, settings) -> None:
    problems = []
    checks = (
        ("max_candidate_windows", "max_candidate_windows", "candidate windows"),
        ("template_comparisons_per_family", "max_template_comparisons", "template comparisons per family"),
        ("bootstrap_replicates", "max_bootstrap_replicates", "bootstrap replicates"),
        ("parameter_runs", "max_parameter_runs", "parameter runs"),
    )
    for field, limit_name, what in checks:
        limit = getattr(settings, limit_name)
        if estimate[field] > limit:
            problems.append(f"{estimate[field]} {what} exceed the limit of {limit}")
    if problems:
        raise ComputeBudgetExceeded(problems, estimate)


# --------------------------------------------------------------------------------------- enqueue
def request_analysis(
    ctx: AppContext, measurement_id: str, request: AnalysisRequest, contributor_id: str | None = None
) -> dict:
    """Validate the budget up front, then enqueue. Identical requests share one job."""
    with ctx.sessions() as session:
        m = session.get(Measurement, measurement_id)
        if m is None:
            raise KeyError(measurement_id)
        if m.moderation_status != "public":
            raise MeasurementUnavailable(f"measurement {m.record_code} is {m.moderation_status}")
        revision, summary = m.current_revision, m.recording_summary
    estimate = estimate_cost(summary, request)
    check_budget(estimate, ctx.settings)
    req = request.model_dump(mode="json")
    payload = {
        "measurement_id": measurement_id,
        "metadata_revision": revision,
        "request": req,
        "estimate": estimate,
    }
    key = "analysis:" + canonical_hash({"m": measurement_id, "rev": revision, "request": req})[:48]
    job = jobs.enqueue(ctx, "analysis", payload, key, contributor_id)
    return {"job_id": job.id, "state": job.state, "estimate": estimate}


# ---------------------------------------------------------------------------------- loading inputs
class StoreScienceCache:
    """AnalysisCache interface over the object store: JSON only, immutable, first result wins."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def key(science_key: str) -> str:
        return f"analyses/science/{science_key}.json"

    def get(self, science_key: str) -> dict | None:
        try:
            return strict_loads(self.store.get(self.key(science_key)))
        except KeyError:
            return None

    def put(self, science_key: str, docs: dict) -> None:
        try:
            self.store.put(self.key(science_key), canonical_dumps(docs).encode(), "application/json")
        except ImmutableObjectError:
            pass


def load_recording(ctx: AppContext, m: Measurement, arts: list[Artifact]):
    """Recording from the preserved originals (verified against their SHA-256) or, for
    canonical-only measurements, from the canonical MiniSEED and sidecar. Returns
    (recording, source artifacts, StationXML SHA-256 or None)."""
    limits = ctx.settings.input_limits()
    originals = sorted(
        (a for a in arts if a.category == "original_data" and a.availability == "available"),
        key=lambda a: (a.filename or "", a.sha256 or ""),
    )
    if originals:
        files = [
            UploadFile(
                a.filename or a.kind,
                reconstruct_original(ctx.store.get(a.blob_key), a.stored_encoding, a.sha256),
            )
            for a in originals
        ]
        rec, *_ = parse_waveforms(files, WaveformOptions.model_validate(m.parse_options), limits)
        sources = originals
    else:
        by_kind = {
            a.kind: a for a in arts if a.category == "canonical_recordings" and a.availability == "available"
        }
        mseed_art, side_art = by_kind.get("canonical_miniseed"), by_kind.get("canonical_sidecar")
        if mseed_art is None or side_art is None:
            raise jobs.PermanentJobError(f"{m.record_code}: no stored recording is available for analysis")
        mseed = ctx.store.get(mseed_art.blob_key)
        if sha256_bytes(mseed) != mseed_art.sha256:
            raise RuntimeError(f"{mseed_art.blob_key} failed its SHA-256 check")
        rec = recording_from_canonical(mseed, strict_loads(ctx.store.get(side_art.blob_key)), limits)
        sources = [mseed_art, side_art]
    response_sha = None
    sx = [a for a in arts if a.kind == "original_stationxml"]
    if sx:
        data = ctx.store.get(sx[0].blob_key)
        response_sha = sha256_bytes(data)
        rec, _ = apply_stationxml(rec, parse_stationxml(data), response_sha)
    return rec, sources, response_sha


# ------------------------------------------------------------------------------------ job handler
def _artifact_id(*parts) -> str:
    return "art_" + canonical_hash(list(parts))[:24]


def handle_analysis(ctx: AppContext, claim: jobs.Claim) -> dict:
    t_wall, t_cpu = time.perf_counter(), time.process_time()
    p = claim.payload
    request = AnalysisRequest.model_validate(p["request"])
    check_budget(p["estimate"], ctx.settings)
    with ctx.sessions() as session:
        m = session.get(Measurement, p["measurement_id"])
        if m is None or m.moderation_status != "public":
            raise MeasurementUnavailable(f"measurement {p['measurement_id']} is not available")
        snap = session.get(MetadataRevision, (m.id, p["metadata_revision"]))
        arts = list(
            session.scalars(
                select(Artifact).where(Artifact.measurement_id == m.id, Artifact.run_id.is_(None))
            )
        )
    rec, sources, response_sha = load_recording(ctx, m, arts)
    meta = MeasurementMetadata.model_validate(snap.metadata_doc)
    rec = apply_sensor_metadata(rec, meta.sensor)
    info = RecordInfo(
        record_code=m.record_code,
        measurement_id=m.id,
        metadata_revision=snap.revision,
        latitude=meta.latitude,
        longitude=meta.longitude,
        measurement_context=meta.measurement_context,
        input_hashes={
            "recording_sources": [a.sha256 for a in sources],
            "metadata_snapshot": canonical_hash(snap.metadata_doc),
        },
        response_sha256=response_sha,
        instrument=evaluate_instrument(rec, meta.sensor, request.config.preprocessing.response_correction),
    )
    if not jobs.heartbeat(ctx, claim):
        raise jobs.LeaseLost(claim.job_id)

    cache = StoreScienceCache(ctx.store)
    res = run_analysis(rec, request, info, cache=cache)
    if cache.get(res.key) != res.scientific:  # a concurrent writer won the immutable slot: use its documents
        res = run_analysis(rec, request, info, cache=cache)

    stamp = utc_iso(ctx.clock())
    report_tag = canonical_hash(request.report.model_dump(mode="json"))[:16]
    refs = [a.id for a in sources]
    generated = {"software": "hvsr-analyzer", "version": PACKAGE_VERSION, "job_id": claim.job_id}
    new_arts: list[Artifact] = []

    def add_json(run_id, category, kind, label, method, doc):
        data = canonical_dumps(doc).encode()
        key = blob_key("analyses", sha256_bytes(data))
        ctx.store.put(key, data, "application/json")
        if sha256_bytes(ctx.store.get(key)) != sha256_bytes(data):
            raise RuntimeError(f"stored object {key} failed read-back verification")
        new_arts.append(
            Artifact(
                id=_artifact_id(run_id, kind, method, report_tag),
                measurement_id=m.id,
                run_id=run_id,
                category=category,
                kind=kind,
                label=label,
                filename=None,
                blob_key=key,
                mime_type="application/json",
                byte_size=len(data),
                sha256=sha256_bytes(data),
                stored_sha256=sha256_bytes(data),
                stored_encoding="identity",
                availability="available",
                availability_reason=None,
                generated_by=dict(
                    generated, run_id=run_id, report_options=request.report.model_dump(mode="json")
                ),
                source_refs=refs,
                method=method,
                created_utc=stamp,
            )
        )

    sesame_note = "" if request.report.evaluate_sesame else " - SESAME not requested"
    for method, doc in res.published.items():
        add_json(
            res.run_id, "analysis_runs", "analysis_json", METHOD_LABEL[method] + sesame_note, method, doc
        )
    runs = [
        AnalysisRun(
            run_id=res.run_id,
            measurement_id=m.id,
            metadata_revision=snap.revision,
            science_key=res.key,
            config_hash=request.config.scientific_hash(),
            request=request.model_dump(mode="json"),
            role="primary",
            primary_run_id=None,
            comparison=res.robustness,
            created_utc=stamp,
            job_id=claim.job_id,
        )
    ]
    if res.robustness is not None:
        add_json(
            res.run_id,
            "quality_evaluation",
            "robustness_comparison",
            "Robustness comparison (Lw and 1.5 x Lw)",
            None,
            res.robustness,
        )
    if res.run_b is not None:
        cfg_b = robustness_config(request.config, rec.sampling_rate_hz)
        for method, doc in res.run_b.published.items():
            label = METHOD_LABEL[method].replace("(JSON)", "- run B, 1.5 x Lw (JSON)") + sesame_note
            add_json(res.run_b.run_id, "analysis_runs", "analysis_json", label, method, doc)
        runs.append(
            AnalysisRun(
                run_id=res.run_b.run_id,
                measurement_id=m.id,
                metadata_revision=snap.revision,
                science_key=res.run_b.key,
                config_hash=cfg_b.scientific_hash(),
                request=request.model_copy(update={"config": cfg_b, "robustness_check": False}).model_dump(
                    mode="json"
                ),
                role="robustness_b",
                primary_run_id=res.run_id,
                comparison=None,
                created_utc=stamp,
                job_id=claim.job_id,
            )
        )
    result = {
        "run_id": res.run_id,
        "run_b_id": res.run_b.run_id if res.run_b else None,
        "comparison_id": res.robustness["comparison_id"] if res.robustness else None,
        "artifact_ids": [a.id for a in new_arts],
        "science_cached": res.cached,
        "wall_s": time.perf_counter() - t_wall,
        "cpu_s": time.process_time() - t_cpu,
    }
    _publish(ctx, claim, m.contributor_id, runs, new_arts, p["estimate"], result)
    return {**result, jobs.COMPLETED: True}


def _publish(ctx, claim, contributor_id, runs, arts, estimate, result) -> None:
    now = ctx.clock()
    for attempt in range(2):
        try:
            with ctx.sessions() as session, session.begin():
                session.add_all([r for r in runs if session.get(AnalysisRun, r.run_id) is None])
                session.add_all([a for a in arts if session.get(Artifact, a.id) is None])
                session.add(
                    ComputeUsage(
                        contributor_id=contributor_id,
                        period=period_of(now),
                        kind="analysis",
                        job_id=claim.job_id,
                        estimate=estimate,
                        cpu_s=result["cpu_s"],
                        wall_s=result["wall_s"],
                        created_utc=utc_iso(now),
                    )
                )
                jobs.complete_in(session, claim, result, now.timestamp())
            return
        except IntegrityError:  # a concurrent publisher inserted the same rows first
            if attempt:
                raise


HANDLERS = {"analysis": handle_analysis}
