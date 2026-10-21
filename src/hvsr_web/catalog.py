"""Public search over measurements: record-code prefix and a latitude/longitude box.

Removed measurements are never listed. Coordinates come from each measurement's current metadata
revision.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from .db import AnalysisRun, Measurement, MetadataRevision
from .manifests import manifest_url, measurement_page

MAX_PAGE = 200


@dataclass(frozen=True)
class Query:
    code: str | None = None
    lat_min: float | None = None
    lat_max: float | None = None
    lon_min: float | None = None
    lon_max: float | None = None
    limit: int = 50
    offset: int = 0


def search(session: Session, q: Query) -> tuple[int, list[dict]]:
    lat = MetadataRevision.metadata_doc["latitude"].as_float()
    lon = MetadataRevision.metadata_doc["longitude"].as_float()
    conds = [Measurement.moderation_status != "removed"]
    if q.code:
        conds.append(Measurement.record_code.startswith(q.code.strip().lower(), autoescape=True))
    for col, lo, hi in ((lat, q.lat_min, q.lat_max), (lon, q.lon_min, q.lon_max)):
        if lo is not None:
            conds.append(col >= lo)
        if hi is not None:
            conds.append(col <= hi)
    base = (
        select(Measurement, MetadataRevision)
        .join(
            MetadataRevision,
            and_(
                MetadataRevision.measurement_id == Measurement.id,
                MetadataRevision.revision == Measurement.current_revision,
            ),
        )
        .where(*conds)
    )
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    limit = max(1, min(q.limit, MAX_PAGE))
    rows = session.execute(
        base.order_by(Measurement.created_utc.desc(), Measurement.record_code)
        .limit(limit)
        .offset(max(0, q.offset))
    ).all()
    ids = [m.id for m, _ in rows]
    runs = dict(
        session.execute(
            select(AnalysisRun.measurement_id, func.count())
            .where(AnalysisRun.measurement_id.in_(ids))
            .group_by(AnalysisRun.measurement_id)
        ).all()
    )
    return total, [summary(m, snap, runs.get(m.id, 0)) for m, snap in rows]


def summary(m: Measurement, snap: MetadataRevision, n_runs: int) -> dict:
    rs = m.recording_summary or {}
    return {
        "measurement_id": m.id,
        "record_code": m.record_code,
        "latitude": snap.metadata_doc.get("latitude"),
        "longitude": snap.metadata_doc.get("longitude"),
        "recording_start_utc": rs.get("start_utc"),
        "duration_s": rs.get("duration_s"),
        "attribution": m.licensing.get("attribution"),
        "license_id": m.licensing.get("license_id"),
        "status": m.moderation_status,
        "analysis_runs": n_runs,
        "manifest_url": manifest_url(m.id),
        "page_url": measurement_page(m.id),
    }
