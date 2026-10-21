"""Atomic monthly upload-quota ledger (spec 3.2; docs/quotas.md).

Charging policy: waveform/CSV files are charged by their logical (uncompressed) byte size so that
compression is not a quota bypass; StationXML and photographs are charged by their uploaded size.
Stored bytes and incoming transfer bytes are metered separately and not charged. One accepted
submission is one ledger entry keyed by the client's idempotency key: retries are not charged twice.
Deleting or removing a measurement never refunds allowance.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import QUOTA_LOCK, Contributor, QuotaEntry, QuotaPeriod, lock_key, serialize, utc_iso

ADVICE = (
    "Options: wait for the next UTC month, split the submission across months, or ask an administrator "
    "for a quota expansion. Recordings are never shortened, decimated, or discarded to fit a budget."
)


class QuotaExceeded(Exception):
    def __init__(self, requested: int, remaining: int, period: str):
        super().__init__(
            f"submission needs {requested} bytes but {remaining} remain in {period} (UTC). {ADVICE}"
        )
        self.requested, self.remaining, self.period = requested, remaining, period


@dataclass
class Reservation:
    entry: QuotaEntry
    newly_charged: bool


def period_of(now: dt.datetime) -> str:
    return now.astimezone(dt.UTC).strftime("%Y-%m")


def _limit(contributor: Contributor, settings) -> int:
    if contributor.monthly_quota_bytes is not None:
        return contributor.monthly_quota_bytes
    if contributor.kind == "guest_pool":
        return settings.guest_pool_monthly_bytes
    return settings.monthly_quota_bytes


def _ensure_period(session: Session, contributor_id: str, period: str, limit: int) -> None:
    if session.get(QuotaPeriod, (contributor_id, period)) is None:
        try:
            with session.begin_nested():
                session.add(QuotaPeriod(contributor_id=contributor_id, period=period, limit_bytes=limit))
        except IntegrityError:
            pass
    session.execute(
        update(QuotaPeriod)
        .where(QuotaPeriod.contributor_id == contributor_id, QuotaPeriod.period == period)
        .values(limit_bytes=limit)
    )


def reserve(
    session: Session,
    contributor: Contributor,
    settings,
    idempotency_key: str,
    logical_bytes: int,
    now: dt.datetime,
) -> Reservation:
    """Atomically reserve bytes for one submission. The caller commits the session."""
    serialize(session, lock_key(QUOTA_LOCK, contributor.id))  # concurrent retries of one key
    existing = session.scalar(
        select(QuotaEntry).where(
            QuotaEntry.contributor_id == contributor.id, QuotaEntry.idempotency_key == idempotency_key
        )
    )
    if existing is not None and existing.state != "released":
        return Reservation(existing, False)
    period = period_of(now)
    _ensure_period(session, contributor.id, period, _limit(contributor, settings))
    total = QuotaPeriod.reserved_bytes + QuotaPeriod.committed_bytes + logical_bytes
    res = session.execute(
        update(QuotaPeriod)
        .where(
            QuotaPeriod.contributor_id == contributor.id,
            QuotaPeriod.period == period,
            total <= QuotaPeriod.limit_bytes,
        )
        .values(reserved_bytes=QuotaPeriod.reserved_bytes + logical_bytes)
    )
    if res.rowcount != 1:
        p = session.get(QuotaPeriod, (contributor.id, period))
        session.refresh(p)
        raise QuotaExceeded(
            logical_bytes, max(0, p.limit_bytes - p.reserved_bytes - p.committed_bytes), period
        )
    stamp = utc_iso(now)
    if existing is not None:  # a released reservation being retried
        existing.state, existing.period, existing.logical_bytes, existing.updated_utc = (
            "reserved",
            period,
            logical_bytes,
            stamp,
        )
        session.flush()
        return Reservation(existing, True)
    entry = QuotaEntry(
        contributor_id=contributor.id,
        period=period,
        idempotency_key=idempotency_key,
        state="reserved",
        logical_bytes=logical_bytes,
        created_utc=stamp,
        updated_utc=stamp,
    )
    session.add(entry)
    session.flush()
    return Reservation(entry, True)


def commit(
    session: Session, entry: QuotaEntry, stored_bytes: int, transfer_bytes: int, now: dt.datetime
) -> bool:
    """Charge a reservation; False if it was already charged."""
    if entry.state == "committed":
        return False
    if entry.state != "reserved":
        raise RuntimeError(f"cannot commit a {entry.state} reservation")
    session.execute(
        update(QuotaPeriod)
        .where(QuotaPeriod.contributor_id == entry.contributor_id, QuotaPeriod.period == entry.period)
        .values(
            reserved_bytes=QuotaPeriod.reserved_bytes - entry.logical_bytes,
            committed_bytes=QuotaPeriod.committed_bytes + entry.logical_bytes,
        )
    )
    entry.state, entry.stored_bytes, entry.transfer_bytes, entry.updated_utc = (
        "committed",
        stored_bytes,
        transfer_bytes,
        utc_iso(now),
    )
    session.flush()
    return True


def release(session: Session, entry: QuotaEntry, now: dt.datetime, note: str) -> None:
    """Roll back a reservation for a submission that failed before it was accepted."""
    if entry.state != "reserved":
        return
    session.execute(
        update(QuotaPeriod)
        .where(QuotaPeriod.contributor_id == entry.contributor_id, QuotaPeriod.period == entry.period)
        .values(reserved_bytes=QuotaPeriod.reserved_bytes - entry.logical_bytes)
    )
    entry.state, entry.updated_utc, entry.note = "released", utc_iso(now), note[:2000]
    session.flush()


def usage(session: Session, contributor: Contributor, settings, now: dt.datetime) -> dict:
    period = period_of(now)
    p = session.get(QuotaPeriod, (contributor.id, period))
    limit = _limit(contributor, settings)
    used = 0 if p is None else p.reserved_bytes + p.committed_bytes
    return {
        "period_utc": period,
        "limit_bytes": limit,
        "committed_bytes": 0 if p is None else p.committed_bytes,
        "reserved_bytes": 0 if p is None else p.reserved_bytes,
        "remaining_bytes": max(0, limit - used),
        "basis": "logical uncompressed waveform bytes plus uploaded StationXML/photo bytes",
        "label": "operational default, not a scientific standard",
        "guest_pool": contributor.kind == "guest_pool",
    }
