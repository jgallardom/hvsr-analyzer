"""Moderation: public reports, administrator removal and restoration, and an explicit purge.

Removal stops distribution from this repository. It cannot recall copies that were already
downloaded and does not revoke CC BY 4.0 rights already granted (licensing.REMOVAL_NOTICE). Quota
charges are never refunded. Purge permanently deletes stored objects that no other measurement
references; it is separate from removal, requires the record code typed back as confirmation, and
is itself recorded.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Admin, AnalysisRun, Artifact, Measurement, ModerationEvent, utc_iso
from .licensing import REMOVAL_NOTICE

PUBLIC_REPORT = "public_report"


class ModerationError(ValueError):
    pass


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason or len(reason) > 2000:
        raise ModerationError("a reason of 1 to 2000 characters is required")
    return reason


def _history(m: Measurement, action: str, reason: str, stamp: str) -> None:
    lic = dict(m.licensing)
    lic["modification_history"] = [
        *lic["modification_history"],
        {"utc": stamp, "action": action, "reason": reason, "metadata_revision": m.current_revision},
    ]
    m.licensing = lic


def _event(session, m, action, reason, actor, stamp, notice=None) -> ModerationEvent:
    ev = ModerationEvent(
        measurement_id=m.id, action=action, reason=reason, actor=actor, created_utc=stamp, notice=notice
    )
    session.add(ev)
    return ev


def report(session: Session, measurement_id: str, reason: str, now: dt.datetime) -> ModerationEvent:
    """Anyone may report; the measurement stays available while an administrator reviews it."""
    m = session.get(Measurement, measurement_id)
    if m is None or m.moderation_status == "removed":
        raise ModerationError("no public measurement with that identifier")
    if m.moderation_status == "public":
        m.moderation_status = "under_review"
    return _event(session, m, "report", _reason(reason), PUBLIC_REPORT, utc_iso(now))


def remove(
    session: Session, admin: Admin, measurement_id: str, reason: str, now: dt.datetime
) -> ModerationEvent:
    m = session.get(Measurement, measurement_id)
    if m is None:
        raise KeyError(measurement_id)
    if m.moderation_status == "removed":
        raise ModerationError(f"{m.record_code} is already removed")
    stamp, reason = utc_iso(now), _reason(reason)
    m.moderation_status = "removed"
    _history(m, "removed", reason, stamp)
    return _event(session, m, "remove", reason, admin.id, stamp, REMOVAL_NOTICE)


def restore(
    session: Session, admin: Admin, measurement_id: str, reason: str, now: dt.datetime
) -> ModerationEvent:
    m = session.get(Measurement, measurement_id)
    if m is None:
        raise KeyError(measurement_id)
    if m.moderation_status == "public":
        raise ModerationError(f"{m.record_code} is already public")
    purged = session.scalar(
        select(func.count())
        .select_from(ModerationEvent)
        .where(ModerationEvent.measurement_id == m.id, ModerationEvent.action == "purge")
    )
    if purged:
        raise ModerationError(f"{m.record_code} was purged; its stored data no longer exist")
    stamp, reason = utc_iso(now), _reason(reason)
    m.moderation_status = "public"
    _history(m, "restored", reason, stamp)
    return _event(session, m, "restore", reason, admin.id, stamp)


def purge(ctx, admin: Admin, measurement_id: str, confirm_record_code: str, reason: str) -> dict:
    """Permanently delete this measurement's stored objects that no other measurement references.

    Only for removed measurements, and only when `confirm_record_code` equals the record code.
    Database rows are kept (marked removed and unavailable) so the record code is never reused and
    the removal history stays auditable.
    """
    stamp = utc_iso(ctx.clock())
    reason = _reason(reason)
    with ctx.sessions() as session, session.begin():
        m = session.get(Measurement, measurement_id)
        if m is None:
            raise KeyError(measurement_id)
        if m.moderation_status != "removed":
            raise ModerationError("only a removed measurement can be purged; remove it first")
        if confirm_record_code != m.record_code:
            raise ModerationError("purge not confirmed: type the record code exactly")
        arts = session.scalars(select(Artifact).where(Artifact.measurement_id == m.id)).all()
        keys = {a.blob_key for a in arts if a.blob_key}
        shared = {
            k
            for (k,) in session.execute(
                select(Artifact.blob_key).where(Artifact.blob_key.in_(keys), Artifact.measurement_id != m.id)
            )
        }
        science = {
            f"analyses/science/{k}.json"
            for (k,) in session.execute(
                select(AnalysisRun.science_key).where(AnalysisRun.measurement_id == m.id)
            )
        }
        doomed = sorted((keys - shared) | science)
        for a in arts:
            a.availability = "removed"
            a.availability_reason = "purged by an administrator"
            a.blob_key = None
        _history(m, "purged", reason, stamp)
        _event(session, m, "purge", reason, admin.id, stamp, REMOVAL_NOTICE)
    deleted = []
    for k in doomed:  # after the commit: a failure leaves unreferenced objects, never dangling rows
        if ctx.store.exists(k):
            ctx.store.delete(k)
            deleted.append(k)
    with ctx.sessions() as session:
        remaining = session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.blob_key.in_(doomed))
        )
    return {
        "deleted_objects": deleted,
        "kept_shared_objects": sorted(shared),
        "dangling_references": remaining,
    }
