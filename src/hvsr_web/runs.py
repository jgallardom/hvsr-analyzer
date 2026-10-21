"""Read side of a published run (for the API and the pages) and cosmetic re-rendering.

A cosmetic change creates a NEW plot artifact that references the existing analysis JSON; it never
reruns FFTs, selection, or the bootstrap (spec 13.2). Identical recipes map to the same artifact.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hvsr_core import plots
from hvsr_core.config import DisplayOptions
from hvsr_core.jsonutil import sha256_bytes, strict_loads
from hvsr_core.strings import t
from hvsr_core.versions import PACKAGE_VERSION

from .context import AppContext
from .db import AnalysisRun, Artifact, Measurement, utc_iso
from .publication import MIME, plot_artifact_id, stored_artifact

PLOT_KIND = {
    ("png", "desktop"): "plot_png",
    ("svg", "desktop"): "plot_svg",
    ("svg", "compact"): "plot_svg_compact",
    ("png", "compact"): "plot_png_compact",
}


class PlotLimitExceeded(Exception):
    """A cosmetic render over the configured figure size (422) or stored-variant count (429)."""

    def __init__(self, error: str, detail: str, status: int):
        super().__init__(detail)
        self.error, self.status = error, status


class WindowsNotReady(Exception):
    """Individual windows were requested but the per-window spectra are not stored (yet)."""

    def __init__(self, artifact: Artifact | None):
        super().__init__(artifact.availability_reason if artifact else "no per-window spectra for this run")
        self.artifact = artifact


class Removed(Exception):
    pass


@dataclass
class RunBundle:
    run: AnalysisRun
    measurement: Measurement
    artifacts: list[Artifact]
    docs: dict[str, tuple[Artifact, dict]]

    def by_method(self, method: str | None) -> dict[str, Artifact]:
        """The artifact of each kind published with the run for one method (None: per-run artifacts);
        cosmetic variants are excluded."""
        out: dict[str, Artifact] = {}
        for a in self.artifacts:
            if (
                a.method == method
                and a.kind not in out
                and not (a.generated_by or {}).get("cosmetic_rerender")
            ):
                out[a.kind] = a
        return out


def verified_bytes(ctx: AppContext, a: Artifact) -> bytes:
    data = ctx.store.get(a.blob_key)
    if sha256_bytes(data) != a.stored_sha256:
        raise RuntimeError(f"artifact {a.id} failed its SHA-256 check")
    return data


def load_run(ctx: AppContext, run_id: str) -> RunBundle:
    with ctx.sessions() as session:
        run = session.get(AnalysisRun, run_id)
        if run is None:
            raise KeyError(run_id)
        m = session.get(Measurement, run.measurement_id)
        arts = list(
            session.scalars(
                select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_utc, Artifact.id)
            )
        )
    if m.moderation_status == "removed":
        raise Removed(run_id)
    docs: dict[str, tuple[Artifact, dict]] = {}
    for a in arts:
        if a.kind == "analysis_json" and a.method not in docs and a.availability == "available":
            docs[a.method] = (a, strict_loads(verified_bytes(ctx, a)))
    return RunBundle(run, m, arts, docs)


def _custom_size(recipe: dict) -> bool:
    default = DisplayOptions()
    size = plots.figure_size_in(default.model_dump(), recipe["layout"])
    return (recipe["width_in"], recipe["height_in"]) != size or recipe["dpi"] != default.dpi


def alternate_method(docs: dict, method: str) -> str | None:
    other = "standard" if method != "standard" else "sdof"
    return other if other in docs else None


def rerender_plot(
    ctx: AppContext,
    run_id: str,
    method: str,
    display: DisplayOptions,
    fmt: str = "svg",
    layout: str = "desktop",
    selection_id: str | None = None,
) -> tuple[Artifact, bool]:
    """(artifact, created). Raises KeyError for an unknown run, method, or selection.

    Variants at the default size and resolution form a finite set (selection x overlays x format x
    layout), so only custom-size variants count toward the per-run cap. The cap is checked before
    rendering, not atomically: simultaneous requests can exceed it by the number of such requests."""
    bundle = load_run(ctx, run_id)
    if method not in bundle.docs or (fmt, layout) not in PLOT_KIND:
        raise KeyError(method)
    limits = ctx.settings
    pixels = plots.raster_pixels(display.model_dump(mode="json"), layout)
    if fmt == "png" and pixels > limits.max_plot_pixels:
        raise PlotLimitExceeded(
            "plot_pixels",
            f"{pixels} pixels exceed the limit of {limits.max_plot_pixels}; reduce the figure size or dpi",
            422,
        )
    analysis, doc = bundle.docs[method]
    if selection_id is not None and selection_id not in {s["selection_id"] for s in doc["selections"]}:
        raise KeyError(selection_id)
    windows, refs = None, [analysis.id]
    if display.show_individual_windows:
        ws = bundle.by_method(None).get("window_spectra")
        if ws is None or ws.availability != "available":
            raise WindowsNotReady(ws)
        sel = plots.main_selection(doc, selection_id)
        freq = np.asarray(doc["grid"]["frequency_hz"], dtype=float)
        windows = plots.window_curves(verified_bytes(ctx, ws), sel["window_ids"], freq)
        refs.append(ws.id)
    alt = alternate_method(bundle.docs, method)
    recipe = plots.make_recipe(
        doc,
        fmt=fmt,
        layout=layout,
        display=display.model_dump(mode="json"),
        selection_id=selection_id,
        alternate_method=alt,
        windows_available=windows is not None,
        source_sha256=analysis.sha256,
    )
    kind = PLOT_KIND[(fmt, layout)]
    art_id = plot_artifact_id(run_id, kind, method, recipe)
    with ctx.sessions() as session:
        existing = session.get(Artifact, art_id)
    if existing is not None:
        return existing, False
    if _custom_size(recipe):
        stored = sum(
            1
            for a in bundle.artifacts
            if (a.generated_by or {}).get("cosmetic_rerender") and _custom_size(a.generated_by["recipe"])
        )
        if stored >= limits.max_plot_variants_per_run:
            raise PlotLimitExceeded(
                "plot_variant_limit",
                f"this run already has {stored} custom-size plot variants (limit "
                f"{limits.max_plot_variants_per_run}); use the default size and resolution",
                429,
            )
    image = plots.render(doc, recipe, alternate_doc=bundle.docs[alt][1] if alt else None, windows=windows)
    suffix = t("artifact.run_b_suffix") if bundle.run.role == "robustness_b" else ""
    art = stored_artifact(
        ctx,
        art_id=art_id,
        measurement_id=bundle.measurement.id,
        run_id=run_id,
        category="plots",
        kind=kind,
        label=t(f"artifact.{kind}", method=t(f"method.{method}")) + suffix,
        method=method,
        data=image,
        mime=MIME[fmt],
        obj_category="plots",
        generated_by={
            "software": "hvsr-analyzer",
            "version": PACKAGE_VERSION,
            "run_id": run_id,
            "cosmetic_rerender": True,
            "recipe": recipe,
        },
        source_refs=refs,
        stamp=utc_iso(ctx.clock()),
    )
    try:
        with ctx.sessions() as session, session.begin():
            session.add(art)
    except IntegrityError:  # the same recipe was rendered concurrently
        with ctx.sessions() as session:
            return session.get(Artifact, art_id), False
    with ctx.sessions() as session:
        return session.get(Artifact, art_id), True
