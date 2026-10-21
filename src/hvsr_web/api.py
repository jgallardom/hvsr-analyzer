"""HTTP API (JSON). Stable artifact endpoints issue fresh short-lived signed URLs on every request.

Upload and administrator tokens are accepted only in the `Authorization: Bearer` header; a
request carrying a credential-like query parameter is rejected so tokens never appear in URLs,
logs, or browser history. Error `detail` strings are English diagnostics next to stable machine
codes (`error`).
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, text

from hvsr_core import exports
from hvsr_core.config import AnalysisRequest, DisplayOptions
from hvsr_core.io.limits import InputLimits
from hvsr_core.io.preservation import reconstruct_original
from hvsr_core.io.stationxml import apply_stationxml, parse_stationxml
from hvsr_core.jsonutil import sha256_bytes
from hvsr_core.model import InputError
from hvsr_core.schema_validation import SCHEMA_FILES, load_schema
from hvsr_core.versions import PACKAGE_VERSION

from . import analysis_jobs, banks, catalog, isolation, moderation, ondemand, pages, runs, worker
from .auth import GUEST_POLICY, AuthError, admin_for_token, contributor_for_token, guest_pool
from .context import AppContext
from .db import AnalysisRun, Artifact, Job, Measurement
from .licensing import CONSENT_TEXT, CONSENT_VERSION, REMOVAL_NOTICE, ConsentRequired
from .manifests import artifact_entry, build_manifest, manifest_url
from .quota import QuotaExceeded
from .settings import Settings
from .storage import LocalObjectStore
from .submissions import (
    LimitExceeded,
    SubmissionRequest,
    WaveformOptions,
    parse_waveforms,
    submit,
    summarize_recording,
)
from .submissions import UploadFile as SubmittedFile

CREDENTIAL_PARAMS = {"token", "access_token", "upload_token", "admin_token", "api_key", "apikey", "password"}
EXT = {
    "application/json": ".json",
    "text/csv": ".csv",
    "application/gzip": ".csv.gz",
    "application/vnd.fdsn.mseed": ".mseed",
    "application/xml": ".xml",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
}
RETRY_AFTER_S = 5
STATIC_DIR = Path(__file__).parent / "static"


def download_name(m: Measurement, a: Artifact) -> str:
    if a.category in ("original_data", "templates") and a.filename:
        return a.filename
    parts = [m.record_code, a.kind] + ([a.method] if a.method else []) + ([a.run_id] if a.run_id else [])
    return "_".join(parts) + EXT.get(a.mime_type, "")


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() or None if h.lower().startswith("bearer ") else None


def job_url(job_id: str) -> str:
    return f"/api/v1/jobs/{job_id}"


def run_url(run_id: str) -> str:
    return f"/api/v1/runs/{run_id}"


def _last_line(error: str | None) -> str | None:
    lines = [ln for ln in (error or "").strip().splitlines() if ln.strip()]
    return lines[-1] if lines else None


class PlotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(description="Method key of the run's analysis (standard, sdof, ...).")
    display: DisplayOptions = Field(default_factory=DisplayOptions)
    format: Literal["png", "svg"] = "svg"
    layout: Literal["desktop", "compact"] = "desktop"
    selection_id: str | None = None


def create_app(ctx: AppContext | None = None) -> FastAPI:
    ctx = ctx or AppContext.from_settings(Settings.from_env())
    isolation.pin_threads(ctx.settings.numeric_threads)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        embedded = worker.start_embedded(ctx) if ctx.settings.embedded_worker else None
        try:
            yield
        finally:
            if embedded is not None:
                thread, stop = embedded
                stop.set()
                thread.join(timeout=30)

    app = FastAPI(title="HVSR Analyzer", version=PACKAGE_VERSION, lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def no_credentials_in_urls(request: Request, call_next):
        if CREDENTIAL_PARAMS & {k.lower() for k in request.query_params}:
            return JSONResponse(
                {
                    "error": "credentials_in_url",
                    "detail": "Send tokens only in the Authorization header (Bearer); never in a URL.",
                },
                status_code=400,
            )
        return await call_next(request)

    @app.exception_handler(AuthError)
    async def _auth(_req, exc: AuthError):
        return JSONResponse({"error": "unauthorized", "detail": str(exc)}, status_code=401)

    @app.exception_handler(UnicodeDecodeError)
    async def _encoding(_req, _exc: UnicodeDecodeError):
        return JSONResponse(
            {"error": "invalid_encoding", "detail": "request body is not UTF-8 JSON"}, status_code=422
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": PACKAGE_VERSION}

    @app.get("/readyz")
    def readyz():
        try:
            with ctx.sessions() as session:
                session.execute(text("SELECT 1"))
            ctx.store.exists("readyz-probe")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "unavailable", "detail": type(exc).__name__}, status_code=503)
        return {"status": "ready"}

    @app.get("/api/v1/limits")
    def limits():
        return {
            "limits": ctx.settings.public_limits(),
            "guest_policy": GUEST_POLICY if ctx.settings.guest_uploads_enabled else None,
            "consent": {"version": CONSENT_VERSION, "text": CONSENT_TEXT},
        }

    # ------------------------------------------------------------------------------ public reads
    @app.get("/api/v1/measurements")
    def measurements(
        code: str | None = Query(None, max_length=40, description="Record-code prefix"),
        lat_min: float | None = Query(None, ge=-90, le=90),
        lat_max: float | None = Query(None, ge=-90, le=90),
        lon_min: float | None = Query(None, ge=-180, le=180),
        lon_max: float | None = Query(None, ge=-180, le=180),
        limit: int = Query(50, ge=1, le=catalog.MAX_PAGE),
        offset: int = Query(0, ge=0),
    ):
        q = catalog.Query(code, lat_min, lat_max, lon_min, lon_max, limit, offset)
        with ctx.sessions() as session:
            total, items = catalog.search(session, q)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @app.get("/api/v1/measurements/{measurement_id}/manifest")
    def manifest(measurement_id: str):
        with ctx.sessions() as session:
            try:
                return build_manifest(
                    session, measurement_id, ctx.settings.moderation_contact, ctx.settings.public_base_url
                )
            except KeyError:
                raise HTTPException(404, "no such measurement") from None

    @app.get("/api/v1/runs/{run_id}")
    def run(run_id: str):
        try:
            b = runs.load_run(ctx, run_id)
        except KeyError:
            raise HTTPException(404, "no such run") from None
        except runs.Removed:
            raise HTTPException(410, "removed from public distribution") from None
        r = b.run
        methods: dict[str, dict] = {}
        for a in b.artifacts:
            if a.method is not None:
                methods.setdefault(a.method, {"artifacts": []})["artifacts"].append(artifact_entry(a, False))
        for method, (a, _doc) in b.docs.items():
            methods[method]["analysis_url"] = f"/api/v1/artifacts/{a.id}"
        return {
            "run_id": r.run_id,
            "measurement_id": r.measurement_id,
            "record_code": b.measurement.record_code,
            "role": r.role,
            "primary_run_id": r.primary_run_id,
            "metadata_revision": r.metadata_revision,
            "science_key": r.science_key,
            "config_hash": r.config_hash,
            "created_utc": r.created_utc,
            "request": r.request,
            "comparison_url": f"/api/v1/comparisons/{r.comparison['comparison_id']}"
            if r.comparison
            else None,
            "manifest_url": manifest_url(r.measurement_id),
            "page_url": f"/runs/{r.run_id}",
            "methods": methods,
            "run_artifacts": [artifact_entry(a, False) for a in b.artifacts if a.method is None],
        }

    @app.get("/api/v1/schemas")
    def schemas():
        return {"schemas": {name: f"/api/v1/schemas/{name}" for name in sorted(SCHEMA_FILES)}}

    @app.get("/api/v1/schemas/{name}")
    def schema(name: str):
        if name not in SCHEMA_FILES:
            raise HTTPException(404, "no such schema")
        return JSONResponse(load_schema(name), media_type="application/schema+json")

    @app.get("/api/v1/data-dictionary")
    def data_dictionary():
        return exports.data_dictionary()

    @app.get("/api/v1/artifacts/{artifact_id}")
    def artifact(artifact_id: str):
        with ctx.sessions() as session:
            a = session.get(Artifact, artifact_id)
            m = session.get(Measurement, a.measurement_id) if a else None
        if a is None:
            raise HTTPException(404, "no such artifact")
        if m.moderation_status == "removed" or a.availability == "removed":
            return JSONResponse(
                {"error": "removed", "detail": "removed from public distribution", "notice": REMOVAL_NOTICE},
                status_code=410,
            )
        if a.availability == "on_demand":
            job = ondemand.request_generation(ctx, a)
            if job.state == "dead":
                return JSONResponse(
                    {
                        "error": "generation_failed",
                        "detail": _last_line(job.last_error),
                        "job_id": job.id,
                        "job_url": job_url(job.id),
                    },
                    status_code=503,
                )
            return JSONResponse(
                {
                    "status": "generating",
                    "detail": a.availability_reason,
                    "label": a.label,
                    "job_id": job.id,
                    "job_url": job_url(job.id),
                    "artifact_url": f"/api/v1/artifacts/{a.id}",
                },
                status_code=202,
                headers={"Retry-After": str(RETRY_AFTER_S), "Cache-Control": "no-store"},
            )
        if a.availability != "available" or not a.blob_key:
            return JSONResponse(
                {"error": "unavailable", "detail": a.availability_reason, "sha256": a.sha256}, status_code=404
            )
        name = download_name(m, a)
        headers = {"Cache-Control": "no-store"}
        if (
            a.stored_encoding == "gzip"
        ):  # plain CSV kept as deterministic gzip: serve the exact original bytes
            limits: InputLimits = ctx.settings.input_limits()
            stored = ctx.store.get(a.blob_key)
            data = reconstruct_original(stored, "gzip", a.sha256)
            if len(data) > limits.max_decoded_bytes:
                raise HTTPException(413, "original exceeds the decoded-size limit")
            headers["Content-Disposition"] = f'attachment; filename="{name}"'
            return Response(data, media_type=a.mime_type, headers=headers)
        url = ctx.store.signed_url(a.blob_key, ctx.settings.signed_url_ttl_s, name, a.mime_type)
        return RedirectResponse(url, status_code=307, headers=headers)

    @app.get("/api/v1/blobs/{key:path}")
    def blob(key: str, exp: int, sig: str, filename: str | None = None, type: str | None = None):  # noqa: A002
        store = ctx.store
        if not isinstance(store, LocalObjectStore):
            raise HTTPException(404, "signed blob URLs are served by the object store")
        if not store.verify_signature(key, exp, sig, filename, type):
            raise HTTPException(403, "signed URL invalid or expired; request the stable artifact URL again")
        try:
            data = store.get(key)
        except KeyError:
            raise HTTPException(404, "object not found") from None
        headers = {"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"}
        if filename:
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return Response(data, media_type=type or "application/octet-stream", headers=headers)

    @app.get("/api/v1/comparisons/{comparison_id}")
    def comparison(comparison_id: str):
        with ctx.sessions() as session:
            for r in session.scalars(select(AnalysisRun).where(AnalysisRun.role == "primary")):
                if r.comparison and r.comparison.get("comparison_id") == comparison_id:
                    m = session.get(Measurement, r.measurement_id)
                    if m.moderation_status == "removed":
                        raise HTTPException(410, "removed from public distribution")
                    return r.comparison
        raise HTTPException(404, "no such comparison")

    # ------------------------------------------------------------------------- contributor writes
    def _contributor(request: Request, allow_guest: bool):
        token = _bearer(request)
        with ctx.sessions() as session, session.begin():
            if token or not allow_guest:
                return contributor_for_token(session, token)
            return guest_pool(session, ctx.settings.guest_uploads_enabled)

    def _check_size(request: Request) -> None:
        s = ctx.settings
        length = int(request.headers.get("content-length") or 0)
        if length > s.max_request_bytes + 1024 * 1024:
            raise HTTPException(413, f"request exceeds {s.max_request_bytes} bytes")

    upload_slots = threading.BoundedSemaphore(ctx.settings.max_concurrent_uploads)

    async def _in_upload_slot(work):
        """Run `work` (read uploaded files into memory and parse them) off the event loop, at most
        `max_concurrent_uploads` at a time; waiting requests keep their bodies in the on-disk spool."""

        def slot():
            if not upload_slots.acquire(timeout=ctx.settings.upload_wait_s):
                raise HTTPException(
                    503,
                    {
                        "error": "busy",
                        "detail": f"{ctx.settings.max_concurrent_uploads} upload(s) are being processed; "
                        "retry shortly",
                    },
                    headers={"Retry-After": "10"},
                )
            try:
                return work()
            finally:
                upload_slots.release()

        return await run_in_threadpool(slot)

    def _uploads(files: list[UploadFile], roles: list[str] | None) -> list[SubmittedFile]:
        if len(files) > ctx.settings.max_files:
            raise HTTPException(
                413, {"error": "file_limit", "detail": f"{len(files)} files exceed {ctx.settings.max_files}"}
            )
        roles = roles or ["waveform"] * len(files)
        if len(roles) != len(files):
            raise HTTPException(422, "one role per file is required")
        return [
            SubmittedFile(f.filename or "upload", f.file.read(), r) for f, r in zip(files, roles, strict=True)
        ]

    @app.post("/api/v1/previews")
    async def preview(
        request: Request,
        waveform: Annotated[str, Form(description="WaveformOptions as JSON")],
        files: Annotated[list[UploadFile], File()],
        roles: Annotated[list[str] | None, Form()] = None,
    ):
        """Parse and validate a recording without storing or charging anything."""
        _check_size(request)
        _contributor(request, allow_guest=True)
        try:
            opts = WaveformOptions.model_validate(json.loads(waveform))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, {"error": "invalid_options", "detail": str(exc)}) from None

        def work():
            uploads = _uploads(files, roles)
            rec, csv_info, _, fmts = parse_waveforms(uploads, opts, ctx.settings.input_limits())
            sx = [f for f in uploads if f.role == "stationxml"]
            if sx:
                rec, _ = apply_stationxml(rec, parse_stationxml(sx[0].data), sha256_bytes(sx[0].data))
            return rec, csv_info, fmts

        try:
            rec, csv_info, fmts = await _in_upload_slot(work)
        except LimitExceeded as exc:
            raise HTTPException(413, {"error": exc.code, "detail": exc.message}) from None
        except InputError as exc:
            raise HTTPException(422, {"error": exc.code, "detail": exc.message}) from None
        return {
            "ok": True,
            "formats": fmts,
            "csv": None if csv_info is None else {"header": csv_info.header, "delimiter": csv_info.delimiter},
            "summary": summarize_recording(rec),
            "stored": False,
        }

    @app.post("/api/v1/submissions", status_code=201)
    async def submissions(
        request: Request,
        submission: Annotated[str, Form(description="SubmissionRequest as JSON")],
        files: Annotated[list[UploadFile], File()],
        roles: Annotated[list[str] | None, Form()] = None,
    ):
        _check_size(request)
        contributor = _contributor(request, allow_guest=True)
        try:
            req = SubmissionRequest.model_validate(json.loads(submission))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, f"invalid submission: {exc}") from None
        try:
            res = await _in_upload_slot(lambda: submit(ctx, contributor, req, _uploads(files, roles)))
        except ConsentRequired as exc:
            raise HTTPException(422, {"error": "consent_required", "problems": exc.problems}) from None
        except LimitExceeded as exc:
            raise HTTPException(413, {"error": exc.code, "detail": exc.message}) from None
        except QuotaExceeded as exc:
            raise HTTPException(429, {"error": "quota_exceeded", "detail": str(exc)}) from None
        except InputError as exc:
            raise HTTPException(422, {"error": exc.code, "detail": exc.message}) from None
        return {
            "measurement_id": res.measurement_id,
            "record_code": res.record_code,
            "existing": res.existing,
            "charged_logical_bytes": res.charged_logical_bytes,
            "stored_bytes": res.stored_bytes,
            "transfer_bytes": res.transfer_bytes,
            "notes": res.notes,
            "manifest_url": manifest_url(res.measurement_id),
            "page_url": f"/measurements/{res.measurement_id}",
        }

    @app.post("/api/v1/measurements/{measurement_id}/analyses", status_code=202)
    def analyses(measurement_id: str, body: AnalysisRequest, request: Request):
        contributor = _contributor(request, allow_guest=False)
        try:
            out = analysis_jobs.request_analysis(ctx, measurement_id, body, contributor.id)
        except KeyError:
            raise HTTPException(404, "no such measurement") from None
        except analysis_jobs.ComputeBudgetExceeded as exc:
            raise HTTPException(
                422,
                {
                    "error": "compute_budget",
                    "problems": exc.problems,
                    "advice": analysis_jobs.ADVICE,
                    "estimate": exc.estimate,
                },
            ) from None
        except analysis_jobs.MeasurementUnavailable as exc:
            raise HTTPException(410, str(exc)) from None
        except banks.BankUnavailable as exc:
            raise HTTPException(422, {"error": "bank_unavailable", "detail": str(exc)}) from None
        return {**out, "job_url": job_url(out["job_id"])}

    @app.post("/api/v1/runs/{run_id}/plots")
    def plots(run_id: str, body: PlotRequest, request: Request):
        """Cosmetic re-render: a new plot artifact referencing the existing numerical run."""
        _contributor(request, allow_guest=False)
        try:
            a, created = runs.rerender_plot(
                ctx, run_id, body.method, body.display, body.format, body.layout, body.selection_id
            )
        except KeyError as exc:
            raise HTTPException(404, f"no such run, method, or selection: {exc}") from None
        except runs.Removed:
            raise HTTPException(410, "removed from public distribution") from None
        except runs.PlotLimitExceeded as exc:
            raise HTTPException(exc.status, {"error": exc.error, "detail": str(exc)}) from None
        except runs.WindowsNotReady as exc:
            ws = exc.artifact
            raise HTTPException(
                409,
                {
                    "error": "window_spectra_not_stored",
                    "detail": "individual windows need the per-window spectra; request that artifact first",
                    "artifact_url": f"/api/v1/artifacts/{ws.id}" if ws else None,
                    "availability": ws.availability if ws else None,
                    "availability_reason": ws.availability_reason if ws else None,
                },
            ) from None
        return JSONResponse(artifact_entry(a, False), status_code=201 if created else 200)

    @app.get("/api/v1/jobs/{job_id}")
    def job(job_id: str):
        with ctx.sessions() as session:
            j = session.get(Job, job_id)
            if j is None:
                raise HTTPException(404, "no such job")
            return {
                "job_id": j.id,
                "kind": j.kind,
                "state": j.state,
                "attempts": j.attempts,
                "result": j.result,
                "error": _last_line(j.last_error) if j.state == "dead" else None,
            }

    @app.post("/api/v1/measurements/{measurement_id}/reports", status_code=202)
    def report(measurement_id: str, reason: Annotated[str, Form()]):
        with ctx.sessions() as session, session.begin():
            try:
                moderation.report(session, measurement_id, reason, ctx.clock())
            except moderation.ModerationError as exc:
                raise HTTPException(422, str(exc)) from None
        return {"status": "received", "contact": ctx.settings.moderation_contact}

    @app.post("/api/v1/admin/measurements/{measurement_id}/{action}")
    def admin_action(measurement_id: str, action: str, request: Request, reason: Annotated[str, Form()]):
        if action not in ("remove", "restore"):
            raise HTTPException(404, "unknown action")
        with ctx.sessions() as session, session.begin():
            admin = admin_for_token(session, _bearer(request))
            try:
                getattr(moderation, action)(session, admin, measurement_id, reason, ctx.clock())
            except KeyError:
                raise HTTPException(404, "no such measurement") from None
            except moderation.ModerationError as exc:
                raise HTTPException(409, str(exc)) from None
        return {"status": action + "d"}

    @app.get("/api/v1/banks")
    def bank_list():
        return {"banks": banks.listing(ctx), "accepted_licenses": banks.allowed_licenses(ctx)}

    @app.post("/api/v1/admin/banks", status_code=201)
    async def bank_upload(
        request: Request,
        manifest: Annotated[UploadFile, File(description="Bank manifest (JSON)")],
        data: Annotated[UploadFile, File(description="Bank arrays (NPZ, no pickled objects)")],
    ):
        """Validate and store a template bank, inactive until activated. Administrators only."""
        _check_size(request)
        with ctx.sessions() as session:
            admin = admin_for_token(session, _bearer(request))
        limit = ctx.settings.max_bank_bytes

        def work():
            manifest_bytes = manifest.file.read(banks.MAX_MANIFEST_BYTES + 1)
            data_bytes = data.file.read(limit + 1)
            return banks.upload(ctx, admin.id, manifest_bytes, data_bytes)

        try:
            bank, created = await _in_upload_slot(work)
        except banks.BankRejected as exc:
            raise HTTPException(422, {"error": "bank_rejected", "detail": str(exc)}) from None
        return JSONResponse({**bank, "created": created}, status_code=201 if created else 200)

    @app.post("/api/v1/admin/banks/{bank_id}/{action}")
    def bank_activation(bank_id: str, action: str, request: Request):
        if action not in ("activate", "deactivate"):
            raise HTTPException(404, "unknown action")
        with ctx.sessions() as session:
            admin = admin_for_token(session, _bearer(request))
        try:
            return banks.set_active(ctx, admin.id, bank_id, action == "activate")
        except KeyError:
            raise HTTPException(404, "no such uploaded bank") from None

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(pages.router(ctx))
    return app
