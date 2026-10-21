"""Measurement manifests: every artifact listed with a stable API URL (never an expiring signed URL).

Only public fields are emitted: no token hashes, private e-mail addresses, internal object keys, or
storage locations. A removed measurement keeps its record code, licensing history, and removal
notice; its metadata are withheld and every artifact is marked removed.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from hvsr_core.schema_validation import load_schema, validate
from hvsr_core.strings import t
from hvsr_core.versions import PACKAGE_VERSION

from .db import AnalysisRun, Artifact, Measurement, MetadataRevision
from .licensing import REMOVAL_NOTICE

CATEGORIES = tuple(load_schema("manifest")["properties"]["categories"]["required"])
LICENSING_FIELDS = (
    "license_id",
    "license_url",
    "attribution",
    "source_citation",
    "consent_version",
    "consent_text",
    "consent_text_sha256",
    "consent_utc",
    "rights_confirmed",
    "modification_history",
    "removal_notice",
    "third_party_note",
)
# Immutable subset embedded in each published analysis JSON; the manifest holds the current record
# (modification history, moderation state).
ANALYSIS_LICENSING_FIELDS = (
    "license_id",
    "license_url",
    "attribution",
    "source_citation",
    "third_party_note",
)


def artifact_url(artifact_id: str) -> str:
    return f"/api/v1/artifacts/{artifact_id}"


def manifest_url(measurement_id: str) -> str:
    return f"/api/v1/measurements/{measurement_id}/manifest"


def artifact_entry(a: Artifact, removed: bool) -> dict:
    return {
        "artifact_id": a.id,
        "label": a.label,
        "kind": a.kind,
        "url": artifact_url(a.id),
        "mime_type": a.mime_type,
        "byte_size": a.byte_size,
        "sha256": a.sha256,
        "availability": "removed" if removed else a.availability,
        "availability_reason": "removed from public distribution; " + REMOVAL_NOTICE
        if removed
        else a.availability_reason,
        "generated_by": a.generated_by,
        "run_id": a.run_id,
        "method": a.method,
        "source_refs": list(a.source_refs),
    }


def measurement_page(measurement_id: str, base_url: str | None = None) -> str:
    return f"{(base_url or '').rstrip('/')}/measurements/{measurement_id}"


def suggested_citation(m: Measurement, base_url: str | None = None) -> str:
    return t(
        "license.suggested_text",
        attribution=m.licensing["attribution"],
        year=m.created_utc[:4],
        code=m.record_code,
        url=measurement_page(m.id, base_url),
    )


def analysis_licensing(m: Measurement) -> dict:
    return {k: m.licensing.get(k) for k in ANALYSIS_LICENSING_FIELDS} | {
        "record_code": m.record_code,
        "manifest_url": manifest_url(m.id),
    }


def build_manifest(
    session: Session, measurement_id: str, moderation_contact: str | None = None, base_url: str | None = None
) -> dict:
    m = session.get(Measurement, measurement_id)
    if m is None:
        raise KeyError(measurement_id)
    removed = m.moderation_status == "removed"
    snap = session.get(MetadataRevision, (m.id, m.current_revision))
    revisions = session.scalars(
        select(MetadataRevision)
        .where(MetadataRevision.measurement_id == m.id)
        .order_by(MetadataRevision.revision)
    ).all()
    arts = session.scalars(
        select(Artifact).where(Artifact.measurement_id == m.id).order_by(Artifact.created_utc, Artifact.id)
    ).all()
    categories: dict[str, list] = {c: [] for c in CATEGORIES}
    for a in arts:
        categories[a.category].append(artifact_entry(a, removed))
    runs = session.scalars(
        select(AnalysisRun)
        .where(AnalysisRun.measurement_id == m.id)
        .order_by(AnalysisRun.created_utc, AnalysisRun.role, AnalysisRun.run_id)
    ).all()
    doc = {
        "schema_id": "hvsr-analyzer/manifest",
        "schema_version": "2.0.0",
        "measurement_id": m.id,
        "record_code": m.record_code,
        "geohash": m.geohash,
        "decimal_year": m.decimal_year,
        "decimal_year_source": m.decimal_year_source,
        "site_group_id": None if removed else m.site_group_id,
        "created_utc": m.created_utc,
        "metadata_revision": m.current_revision,
        "metadata": {} if removed else snap.metadata_doc,
        "original_bytes_retained": m.original_bytes_retained,
        "original_bytes_note": m.original_bytes_note,
        "categories": categories,
        "licensing": {k: m.licensing[k] for k in LICENSING_FIELDS if k in m.licensing}
        | {"suggested_citation": suggested_citation(m, base_url)},
        "provenance": {
            "software_version": PACKAGE_VERSION,
            "metadata_revisions": [
                {
                    "revision": r.revision,
                    "created_utc": r.created_utc,
                    "reason": r.reason,
                    "attribution": r.author_attribution,
                }
                for r in revisions
            ],
            "analysis_runs": [
                {
                    "run_id": r.run_id,
                    "run_url": f"/api/v1/runs/{r.run_id}",
                    "role": r.role,
                    "primary_run_id": r.primary_run_id,
                    "metadata_revision": r.metadata_revision,
                    "config_hash": r.config_hash,
                    "created_utc": r.created_utc,
                    "comparison_url": f"/api/v1/comparisons/{r.comparison['comparison_id']}"
                    if r.comparison
                    else None,
                }
                for r in runs
            ],
            "recording": None if removed else m.recording_summary,
        },
        "moderation": {"status": m.moderation_status, "contact": moderation_contact},
    }
    validate(doc, "manifest")
    return doc
