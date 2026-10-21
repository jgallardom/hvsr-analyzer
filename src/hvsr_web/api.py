"""HTTP API (JSON). Stable artifact endpoints issue fresh short-lived signed URLs on every request.

Upload and administrator tokens are accepted only in the `Authorization: Bearer` header; a
request carrying a credential-like query parameter is rejected so tokens never appear in URLs,
logs, or browser history.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select, text

from hvsr_core.config import AnalysisRequest
from hvsr_core.io.limits import InputLimits
from hvsr_core.io.preservation import reconstruct_original
from hvsr_core.model import InputError
from hvsr_core.versions import PACKAGE_VERSION

from . import analysis_jobs, moderation
from .auth import GUEST_POLICY, AuthError, admin_for_token, contributor_for_token, guest_pool
from .context import AppContext
from .db import AnalysisRun, Artifact, Job, Measurement
from .licensing import CONSENT_TEXT, CONSENT_VERSION, REMOVAL_NOTICE, ConsentRequired
from .manifests import build_manifest
from .quota import QuotaExceeded
from .settings import Settings
from .storage import LocalObjectStore
from .submissions import LimitExceeded, SubmissionRequest, submit
from .submissions import UploadFile as SubmittedFile

CREDENTIAL_PARAMS = {"token", "access_token", "upload_token", "admin_token", "api_key", "apikey", "password"}
EXT = {
    "application/json": ".json",
    "text/csv": ".csv",
    "application/gzip": ".csv.gz",
    "application/vnd.fdsn.mseed": ".mseed",
    "application/xml": ".xml",
    "image/jpeg": ".jpg",
}


def download_name(m: Measurement, a: Artifact) -> str:
    if a.category == "original_data" and a.filename:
        return a.filename
    parts = [m.record_code, a.kind] + ([a.method] if a.method else []) + ([a.run_id] if a.run_id else [])
    return "_".join(parts) + EXT.get(a.mime_type, "")


def _bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() or None if h.lower().startswith("bearer ") else None


def create_app(ctx: AppContext | None = None) -> FastAPI:
    ctx = ctx or AppContext.from_settings(Settings.from_env())
    app = FastAPI(title="HVSR Analyzer", version=PACKAGE_VERSION)
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

    @app.get("/api/v1/measurements/{measurement_id}/manifest")
    def manifest(measurement_id: str):
        with ctx.sessions() as session:
            try:
                return build_manifest(session, measurement_id, ctx.settings.moderation_contact)
            except KeyError:
                raise HTTPException(404, "no such measurement") from None

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
            return JSONResponse(
                {"error": "on_demand", "detail": a.availability_reason, "label": a.label}, status_code=409
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
        headers = {"Cache-Control": "private, max-age=60"}
        if filename:
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return Response(data, media_type=type or "application/octet-stream", headers=headers)

    @app.get("/api/v1/comparisons/{comparison_id}")
    def comparison(comparison_id: str):
        with ctx.sessions() as session:
            for run in session.scalars(select(AnalysisRun).where(AnalysisRun.role == "primary")):
                if run.comparison and run.comparison.get("comparison_id") == comparison_id:
                    m = session.get(Measurement, run.measurement_id)
                    if m.moderation_status == "removed":
                        raise HTTPException(410, "removed from public distribution")
                    return run.comparison
        raise HTTPException(404, "no such comparison")

    @app.post("/api/v1/submissions", status_code=201)
    async def submissions(
        request: Request,
        submission: Annotated[str, Form(description="SubmissionRequest as JSON")],
        files: Annotated[list[UploadFile], File()],
        roles: Annotated[list[str] | None, Form()] = None,
    ):
        s = ctx.settings
        length = int(request.headers.get("content-length") or 0)
        if length > s.max_request_bytes + 1024 * 1024:
            raise HTTPException(413, f"request exceeds {s.max_request_bytes} bytes")
        token = _bearer(request)
        with ctx.sessions() as session, session.begin():
            contributor = (
                contributor_for_token(session, token)
                if token
                else guest_pool(session, s.guest_uploads_enabled)
            )
        try:
            req = SubmissionRequest.model_validate(json.loads(submission))
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, f"invalid submission: {exc}") from None
        roles = roles or ["waveform"] * len(files)
        if len(roles) != len(files):
            raise HTTPException(422, "one role per file is required")
        uploads = [
            SubmittedFile(f.filename or "upload", await f.read(), r)
            for f, r in zip(files, roles, strict=True)
        ]
        try:
            res = submit(ctx, contributor, req, uploads)
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
            "manifest_url": f"/api/v1/measurements/{res.measurement_id}/manifest",
        }

    @app.post("/api/v1/measurements/{measurement_id}/analyses", status_code=202)
    def analyses(measurement_id: str, body: AnalysisRequest, request: Request):
        token = _bearer(request)
        with ctx.sessions() as session:
            contributor = contributor_for_token(session, token)
        try:
            return analysis_jobs.request_analysis(ctx, measurement_id, body, contributor.id)
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

    return app
