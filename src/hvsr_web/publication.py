"""Saved outputs of a finished run (spec 13.2): written once, immutable, content-addressed.

Per run and method: the analysis JSON, the mean-spectrum CSV, the per-window table (CSV and JSON),
and the figure as PNG, SVG, and a narrow-screen SVG. Per run: per-window spectra, regenerated on
request within the compute limits. Per template bank used: its manifest and data. Every derived
artifact lists the analysis JSON it was produced from in `source_refs`, and figures store their
recipe in `generated_by.recipe`.
"""

from __future__ import annotations

import copy
import json

from hvsr_core import exports, plots
from hvsr_core.jsonutil import canonical_dumps, canonical_hash, sha256_bytes
from hvsr_core.strings import t
from hvsr_core.templates import bank_dirs

from .db import Artifact
from .manifests import analysis_licensing, artifact_url
from .storage import blob_key

MIME = {
    "json": "application/json",
    "csv": "text/csv",
    "csv.gz": "application/gzip",
    "png": "image/png",
    "svg": "image/svg+xml",
    "npz": "application/octet-stream",
}
PLOT_KINDS = (
    ("plot_png", "png", "desktop"),
    ("plot_svg", "svg", "desktop"),
    ("plot_svg_compact", "svg", "compact"),
)


def artifact_id(*parts) -> str:
    return "art_" + canonical_hash(list(parts))[:24]


def store_verified(ctx, category: str, data: bytes, mime: str) -> str:
    key = blob_key(category, sha256_bytes(data))
    ctx.store.put(key, data, mime)
    if sha256_bytes(ctx.store.get(key)) != sha256_bytes(data):
        raise RuntimeError(f"stored object {key} failed read-back verification")
    return key


def stored_artifact(ctx, *, art_id, measurement_id, run_id, category, kind, label, method, data, mime,
                    obj_category, generated_by, source_refs, stamp, filename=None) -> Artifact:  # fmt: skip
    key = store_verified(ctx, obj_category, data, mime)
    sha = sha256_bytes(data)
    return Artifact(
        id=art_id,
        measurement_id=measurement_id,
        run_id=run_id,
        category=category,
        kind=kind,
        label=label[:120],
        filename=filename,
        blob_key=key,
        mime_type=mime,
        byte_size=len(data),
        sha256=sha,
        stored_sha256=sha,
        stored_encoding="identity",
        availability="available",
        availability_reason=None,
        generated_by=generated_by,
        source_refs=list(source_refs),
        method=method,
        created_utc=stamp,
    )


def _alternate(docs: dict, method: str) -> str | None:
    other = "standard" if method != "standard" else "sdof"
    return other if other in docs else None


def run_artifacts(ctx, m, run, request, *, refs, generated, stamp, suffix, report_tag) -> list[Artifact]:
    """Artifacts of one published run (`run` is an AnalysisResult)."""
    sesame_note = "" if request.report.evaluate_sesame else t("artifact.sesame_off_suffix")
    docs = copy.deepcopy(run.published)
    sup = run.supplement
    display = request.display.model_dump(mode="json")
    out: list[Artifact] = []
    gen = dict(generated, run_id=run.run_id)

    def add(kind, method, label, data, fmt, *, category, obj_category, source_refs, art_id=None, extra=None):
        out.append(
            stored_artifact(
                ctx,
                art_id=art_id or artifact_id(run.run_id, kind, method),
                measurement_id=m.id,
                run_id=run.run_id,
                category=category,
                kind=kind,
                label=label + suffix,
                method=method,
                data=data,
                mime=MIME[fmt],
                obj_category=obj_category,
                generated_by=dict(gen, **(extra or {})),
                source_refs=source_refs,
                stamp=stamp,
            )
        )
        return out[-1]

    licensing = analysis_licensing(m)
    for method, doc in docs.items():
        doc["licensing"] = licensing
        if method in sup["window_tables"]:
            doc["windows"]["candidates_url"] = artifact_url(
                artifact_id(run.run_id, "window_table_json", method)
            )
    for method, doc in docs.items():
        name = t(f"method.{method}")
        data = canonical_dumps(doc).encode()
        analysis = add(
            "analysis_json", method, t("artifact.analysis_json", method=name) + sesame_note, data, "json",
            category="analysis_runs", obj_category="analyses", source_refs=refs,
            art_id=artifact_id(run.run_id, "analysis_json", method, report_tag),
            extra={"report_options": request.report.model_dump(mode="json")},
        )  # fmt: skip
        src = [analysis.id, *refs]
        rows = exports.mean_spectrum_rows(doc, sup["grid_invalid_reason"])
        add("mean_spectrum_csv", method, t("artifact.mean_spectrum_csv", method=name),
            exports.table_csv("mean_spectrum", rows), "csv", category="spectra", obj_category="analyses",
            source_refs=src, extra={"table": "mean_spectrum"})  # fmt: skip
        if method in sup["window_tables"]:
            wrows = sup["window_tables"][method]
            table = exports.window_table_name(method)
            add("window_table_csv", method, t("artifact.window_table_csv", method=name),
                exports.table_csv(table, wrows), "csv", category="analysis_runs", obj_category="analyses",
                source_refs=src, extra={"table": table})  # fmt: skip
            add("window_table_json", method, t("artifact.window_table_json", method=name),
                canonical_dumps(exports.window_table_doc(doc, wrows)).encode(), "json",
                category="analysis_runs", obj_category="analyses", source_refs=src,
                extra={"table": table})  # fmt: skip
        if not doc["selections"]:
            continue  # nothing to draw; the analysis JSON states the method status and reason
        alt = _alternate(docs, method)
        for kind, fmt, layout in PLOT_KINDS:
            recipe = plots.make_recipe(
                doc, fmt=fmt, layout=layout, display=display, alternate_method=alt,
                source_sha256=analysis.sha256,
            )  # fmt: skip
            image = plots.render(doc, recipe, alternate_doc=docs.get(alt) if alt else None)
            add(kind, method, t(f"artifact.{kind}", method=name), image, fmt, category="plots",
                obj_category="plots", source_refs=src, art_id=plot_artifact_id(run.run_id, kind, method, recipe),
                extra={"recipe": recipe})  # fmt: skip
    out.append(
        window_spectra_artifact(ctx, m, run, docs, refs=refs, generated=gen, stamp=stamp, suffix=suffix)
    )
    return out


def plot_artifact_id(run_id: str, kind: str, method: str, recipe: dict) -> str:
    return artifact_id(run_id, kind, method, canonical_hash(recipe))


def window_spectra_cells(doc: dict) -> int:
    return int(doc["windows"]["candidate_count"]) * int(sum(doc["grid"]["valid"]))


def window_spectra_artifact(ctx, m, run, docs, *, refs, generated, stamp, suffix) -> Artifact:
    doc = docs["standard"]
    cells = window_spectra_cells(doc)
    limit = ctx.settings.max_window_spectra_cells
    avail, reason = (
        ("on_demand", t("artifact.reason.window_spectra_on_demand"))
        if cells <= limit
        else ("unavailable", t("artifact.reason.window_spectra_too_large", cells=cells, limit=limit))
    )
    return Artifact(
        id=artifact_id(run.run_id, "window_spectra", None),
        measurement_id=m.id,
        run_id=run.run_id,
        category="spectra",
        kind="window_spectra",
        label=t("artifact.window_spectra") + suffix,
        filename=None,
        blob_key=None,
        mime_type=MIME["csv.gz"],
        byte_size=None,
        sha256=None,
        stored_sha256=None,
        stored_encoding=None,
        availability=avail,
        availability_reason=reason,
        generated_by=dict(generated, table="window_spectra", cells=cells, science_key=run.key),
        source_refs=list(refs),
        method=None,
        created_utc=stamp,
    )


def bank_file_bytes(provenance: dict) -> tuple[bytes, bytes]:
    """The shipped (or administrator-installed) bank manifest and data, verified by SHA-256."""
    for d in bank_dirs():
        p = d / f"{provenance['bank_id']}.json"
        if p.is_file():
            manifest_bytes = p.read_bytes()
            data = (d / json.loads(manifest_bytes)["data_file"]).read_bytes()
            if sha256_bytes(data) == provenance["data_sha256"]:
                return manifest_bytes, data
    raise FileNotFoundError(f"template bank {provenance['bank_id']} with the recorded SHA-256 was not found")


def template_artifacts(ctx, m, published: dict, *, generated, stamp) -> list[Artifact]:
    out = []
    for doc in published.values():
        prov = doc["provenance"].get("template_bank")
        if not prov:
            continue
        manifest_bytes, data = bank_file_bytes(prov)
        for kind, payload, fmt, label in (
            ("template_bank_manifest", manifest_bytes, "json", "artifact.template_manifest"),
            ("template_bank_data", data, "npz", "artifact.template_data"),
        ):
            out.append(
                stored_artifact(
                    ctx,
                    art_id=artifact_id(m.id, kind, prov["data_sha256"]),
                    measurement_id=m.id,
                    run_id=None,
                    category="templates",
                    kind=kind,
                    label=t(label, bank_id=prov["bank_id"]),
                    method=None,
                    data=payload,
                    mime=MIME[fmt],
                    obj_category="templates",
                    generated_by=dict(generated, bank=prov),
                    source_refs=[],
                    stamp=stamp,
                    filename=f"{prov['bank_id']}.{fmt}",
                )
            )
    return out
