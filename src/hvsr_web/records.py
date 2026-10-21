"""Record codes and versioned metadata (spec 2.3)."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hvsr_core.identity import record_code, with_suffix
from hvsr_core.metadata import MeasurementMetadata

from .db import Measurement, MetadataRevision, RecordCodeCounter, utc_iso


def identity_for(lat: float, lon: float, recording_start_ns: int | None, upload_time: dt.datetime) -> dict:
    if recording_start_ns is not None:
        ident, source = record_code(lat, lon, recording_start_ns), "recording_start"
    else:
        ident, source = record_code(lat, lon, int(upload_time.timestamp() * 1e9)), "upload_time"
    ident["decimal_year_source"] = source
    return ident


def allocate_record_code(session: Session, base_code: str) -> tuple[str, int]:
    """Next free suffix for a base code inside the caller's transaction. Never reuses or overwrites."""
    for _ in range(3):
        res = session.execute(
            update(RecordCodeCounter)
            .where(RecordCodeCounter.base_code == base_code)
            .values(last_n=RecordCodeCounter.last_n + 1)
        )
        if res.rowcount == 1:
            n = session.scalar(
                select(RecordCodeCounter.last_n).where(RecordCodeCounter.base_code == base_code)
            )
            return with_suffix(base_code, n), n
        try:
            with session.begin_nested():
                session.add(RecordCodeCounter(base_code=base_code, last_n=1))
            return with_suffix(base_code, 1), 1
        except IntegrityError:
            continue
    raise RuntimeError("could not allocate a record code")


def correct_metadata(
    session: Session,
    measurement: Measurement,
    new_metadata: MeasurementMetadata,
    reason: str,
    author_attribution: str,
    now: dt.datetime | None = None,
) -> MetadataRevision:
    """Append a metadata revision. The record code stays as allocated; earlier snapshots are kept."""
    if not reason.strip() or not author_attribution.strip():
        raise ValueError("a correction needs a reason and a public attribution")
    stamp = utc_iso(now)
    rev = MetadataRevision(
        measurement_id=measurement.id,
        revision=measurement.current_revision + 1,
        metadata_doc=new_metadata.model_dump(mode="json"),
        created_utc=stamp,
        reason=reason,
        author_attribution=author_attribution,
    )
    session.add(rev)
    measurement.current_revision = rev.revision
    lic = dict(measurement.licensing)
    lic["modification_history"] = [
        *lic["modification_history"],
        {
            "utc": stamp,
            "action": "metadata_corrected",
            "metadata_revision": rev.revision,
            "reason": reason,
            "attribution": author_attribution,
        },
    ]
    measurement.licensing = lic
    measurement.site_group_id = new_metadata.site_group_id
    session.flush()
    return rev


def metadata_snapshot(session: Session, measurement_id: str, revision: int | None = None) -> MetadataRevision:
    q = select(MetadataRevision).where(MetadataRevision.measurement_id == measurement_id)
    q = (
        q.where(MetadataRevision.revision == revision)
        if revision is not None
        else q.order_by(MetadataRevision.revision.desc())
    )
    rev = session.scalars(q).first()
    if rev is None:
        raise KeyError(f"no metadata revision {revision} for {measurement_id}")
    return rev
