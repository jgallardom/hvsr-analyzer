"""Public HTML pages: records and search, upload, measurement, configuration, results, data.

Every visible string comes from the locale catalog through `t()`; JavaScript receives its strings as
JSON. Data-derived text (identifiers, status codes, user-entered metadata, stored artifact labels, and
the English diagnostic sentences of the scientific core and data dictionary) is rendered verbatim
inside `translate="no"` elements, which marks it as content rather than interface text. Pages only
read published data; the one write they can trigger is a cosmetic figure variant (overlay toggles or
another peak family at the default size), which is bounded, idempotent, and references the existing
numerical run.
"""

from __future__ import annotations

import math
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape
from sqlalchemy import select

from hvsr_core import exports, plots
from hvsr_core.config import AnalysisConfig, AnalysisRequest, DisplayOptions
from hvsr_core.schema_validation import SCHEMA_FILES
from hvsr_core.strings import DEFAULT_LANG, format_coordinate, results_title, t
from hvsr_core.templates import available_banks
from hvsr_core.versions import PACKAGE_VERSION

from . import catalog, runs
from .context import AppContext
from .db import AnalysisRun, Artifact, Measurement, MetadataRevision
from .licensing import CONSENT_TEXT, CONSENT_VERSION
from .manifests import CATEGORIES, artifact_url, manifest_url, suggested_citation

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
JS_KEYS = (
    "upload.checking",
    "upload.submitting",
    "upload.accepted",
    "upload.open",
    "upload.rejected",
    "upload.token_missing",
    "upload.token_saved",
    "upload.token_forgotten",
    "preview.title",
    "preview.fs",
    "preview.components",
    "preview.start",
    "preview.duration",
    "preview.samples",
    "preview.gaps",
    "preview.clipping",
    "preview.timing",
    "preview.warnings",
    "preview.errors",
    "value.yes",
    "value.no",
    "value.none",
    "configure.state",
    "configure.estimate",
    "configure.budget",
    "configure.done",
    "configure.failed",
    "configure.open_run",
    "download.generating",
    "download.failed",
    "report.sent",
    "error.request_failed",
)
# The narrow-screen SVG is served up to this viewport width (CSS: at most COMPACT_MAX_PX wide, edge to
# edge below the 640 px page breakpoint); above it the desktop SVG fills a column of at least 869 px.
# Together they keep every figure text at >= 9 CSS px from a 340 px viewport upward (AC-S8-07).
FIGURE_BREAKPOINT_PX = 900
COMPACT_MAX_PX = 480


KIND_ORDER = (
    "original_csv",
    "original_csv_gz",
    "original_miniseed",
    "original_miniseed3",
    "canonical_miniseed",
    "canonical_sidecar",
    "canonical_residual_columns",
    "reconstructed_csv",
    "normalized_csv",
    "measurement_metadata",
    "original_stationxml",
    "photo_sensor",
    "photo_area",
    "analysis_json",
    "mean_spectrum_csv",
    "window_table_csv",
    "window_table_json",
    "window_spectra",
    "plot_svg",
    "plot_png",
    "plot_svg_compact",
    "robustness_comparison",
    "template_bank_manifest",
    "template_bank_data",
)


def _ordered(arts) -> list:
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    return sorted(arts, key=lambda a: (rank.get(a.kind, len(rank)), a.label, a.id))


def _num(value, digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return t("value.none")
    if isinstance(value, bool):
        return t("value.yes") if value else t("value.no")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _size(n: int | None) -> str:
    return t("value.none") if n is None else f"{n:,}"


def verbatim(value, empty_key: str = "value.none") -> Markup | str:
    if value is None or value == "":
        return t(empty_key)
    return Markup('<span translate="no">{}</span>').format(escape(value))


def environment() -> Environment:
    env = Environment(
        loader=PackageLoader("hvsr_web", "templates"),
        autoescape=select_autoescape(("html",)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        t=t,
        lang=DEFAULT_LANG,
        version=PACKAGE_VERSION,
        coord=format_coordinate,
        results_title=results_title,
        artifact_url=artifact_url,
        manifest_url=manifest_url,
        breakpoint=FIGURE_BREAKPOINT_PX,
        verbatim=verbatim,
    )
    env.filters.update(num=_num, size=_size)
    return env


def _ok_display(alt: bool, windows: bool) -> DisplayOptions:
    return DisplayOptions(show_alternate_method=alt, show_individual_windows=windows)


def router(ctx: AppContext) -> APIRouter:
    env = environment()
    r = APIRouter(default_response_class=HTMLResponse)

    def page(name: str, status: int = 200, **values) -> HTMLResponse:
        values.setdefault("page_title", t("site.name"))
        values.setdefault("contact", ctx.settings.moderation_contact)
        values.setdefault("js_strings", {k: t(k) for k in JS_KEYS})
        return HTMLResponse(
            env.get_template(name).render(**values), status_code=status, headers=SECURITY_HEADERS
        )

    def not_found(removed: bool = False) -> HTMLResponse:
        msg = t("error.removed") if removed else t("error.not_found")
        return page("error.html", 410 if removed else 404, page_title=t("error.title"), message=msg)

    # ----------------------------------------------------------------------------------- records
    @r.get("/")
    @r.get("/search")
    def records(
        request: Request,
        code: str | None = Query(None, max_length=40),
        lat_min: float | None = Query(None, ge=-90, le=90),
        lat_max: float | None = Query(None, ge=-90, le=90),
        lon_min: float | None = Query(None, ge=-180, le=180),
        lon_max: float | None = Query(None, ge=-180, le=180),
        offset: int = Query(0, ge=0),
    ):
        q = catalog.Query(code or None, lat_min, lat_max, lon_min, lon_max, 50, offset)
        with ctx.sessions() as session:
            total, items = catalog.search(session, q)
        searching = request.url.path == "/search"
        next_url = None
        if offset + len(items) < total:
            params = {k: v for k, v in vars(q).items() if v is not None and k not in ("limit", "offset")}
            next_url = "/search?" + urlencode(params | {"offset": offset + len(items)})
        return page(
            "records.html",
            page_title=t("search.title") if searching else t("records.title"),
            searching=searching,
            q=q,
            total=total,
            items=items,
            next_url=next_url,
        )

    # ------------------------------------------------------------------------------------ upload
    @r.get("/upload")
    def upload():
        return page(
            "upload.html",
            page_title=t("upload.title"),
            consent_text=CONSENT_TEXT,
            consent_version=CONSENT_VERSION,
            limits=ctx.settings.public_limits(),
            levels=("unknown", "none", "low", "moderate", "high"),
            contexts=(
                "free_field_ground",
                "building_ground_floor",
                "building_upper_floor",
                "basement",
                "structure_other",
                "unknown",
            ),
            quantities=("counts", "velocity", "acceleration", "displacement", "other", "unknown"),
            layouts=("elapsed_time", "utc_timestamps", "samples_with_rate"),
        )

    # ------------------------------------------------------------------------------- measurement
    def _measurement(measurement_id: str):
        with ctx.sessions() as session:
            m = session.get(Measurement, measurement_id)
            if m is None:
                return None
            snap = session.get(MetadataRevision, (m.id, m.current_revision))
            arts = list(
                session.scalars(
                    select(Artifact)
                    .where(Artifact.measurement_id == m.id)
                    .order_by(Artifact.created_utc, Artifact.id)
                )
            )
            run_rows = list(
                session.scalars(
                    select(AnalysisRun)
                    .where(AnalysisRun.measurement_id == m.id)
                    .order_by(AnalysisRun.created_utc.desc(), AnalysisRun.role, AnalysisRun.run_id)
                )
            )
        return m, snap, arts, run_rows

    @r.get("/measurements/{measurement_id}")
    def measurement(measurement_id: str):
        found = _measurement(measurement_id)
        if found is None:
            return not_found()
        m, snap, arts, run_rows = found
        if m.moderation_status == "removed":
            return not_found(removed=True)
        meta = snap.metadata_doc
        groups = {c: _ordered(a for a in arts if a.category == c and a.run_id is None) for c in CATEGORIES}
        missing = [a for a in arts if a.category == "original_data" and a.availability != "available"]
        photos = [
            a for a in arts if a.kind in ("photo_sensor", "photo_area") and a.availability == "available"
        ]
        return page(
            "measurement.html",
            page_title=results_title(m.record_code, meta.get("latitude"), meta.get("longitude")),
            m=m,
            meta=meta,
            revision=snap.revision,
            groups={c: v for c, v in groups.items() if v},
            missing=missing,
            photos=photos,
            runs=run_rows,
            citation=suggested_citation(m, ctx.settings.public_base_url),
            summary=m.recording_summary or {},
        )

    @r.get("/measurements/{measurement_id}/configure")
    def configure(measurement_id: str):
        found = _measurement(measurement_id)
        if found is None:
            return not_found()
        m, snap, *_ = found
        if m.moderation_status == "removed":
            return not_found(removed=True)
        meta = snap.metadata_doc
        return page(
            "configure.html",
            page_title=t("configure.title"),
            m=m,
            title=results_title(m.record_code, meta.get("latitude"), meta.get("longitude")),
            defaults=AnalysisRequest().model_dump(mode="json"),
            cfg=AnalysisConfig(),
            banks=[b for b in available_banks() if b.startswith("sdof-")],
            limits=ctx.settings.public_limits(),
        )

    # ----------------------------------------------------------------------------------- results
    @r.get("/runs/{run_id}")
    def results(
        run_id: str,
        method: str | None = Query(None, max_length=40),
        sel: str | None = Query(None, max_length=80),
        alt: bool = False,
        windows: bool = False,
    ):
        try:
            b = runs.load_run(ctx, run_id)
        except KeyError:
            return not_found()
        except runs.Removed:
            return not_found(removed=True)
        if not b.docs:
            return not_found()
        method = method if method in b.docs else ("standard" if "standard" in b.docs else next(iter(b.docs)))
        analysis, doc = b.docs[method]
        selection_ids = [s["selection_id"] for s in doc["selections"]]
        sel = sel if sel in selection_ids else None
        selection = plots.main_selection(doc, sel)
        mine = b.by_method(method)
        per_run = b.by_method(None)
        figure = {"desktop": mine.get("plot_svg"), "compact": mine.get("plot_svg_compact")}
        note = windows_artifact = None

        def variant(show_windows: bool) -> dict:
            display = _ok_display(alt, show_windows)
            return {
                lay: runs.rerender_plot(ctx, run_id, method, display, "svg", lay, sel)[0] for lay in figure
            }

        with_windows = windows
        if windows:
            try:
                figure = variant(True)
            except runs.WindowsNotReady as exc:
                note, windows_artifact, with_windows = t("results.windows_not_ready"), exc.artifact, False
        if not with_windows and (sel is not None or alt):
            figure = variant(False)
        drawn = plots.band_drawn(plots.plotted_series(doc, selection))
        unc = selection["uncertainty"]
        return page(
            "run.html",
            page_title=results_title(
                doc["record"]["record_code"], doc["record"]["latitude"], doc["record"]["longitude"]
            ),
            run=b.run,
            m=b.measurement,
            doc=doc,
            analysis=analysis,
            method=method,
            methods=list(b.docs),
            selection=selection,
            selection_ids=selection_ids,
            sel=sel,
            alt=alt,
            windows=windows,
            figure=figure,
            note=note,
            windows_artifact=windows_artifact,
            drawn=drawn,
            unc=unc,
            files=_ordered(mine.values()) + _ordered(per_run.values()),
            has_alternate=runs.alternate_method(b.docs, method) is not None,
        )

    # -------------------------------------------------------------------------------------- data
    @r.get("/data")
    def data():
        return page(
            "data.html",
            page_title=t("data.title"),
            dictionary=exports.data_dictionary(),
            schemas=sorted(SCHEMA_FILES),
            categories=CATEGORIES,
        )

    return r
