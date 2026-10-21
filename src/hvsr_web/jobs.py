"""Durable job table with atomic claims, leases, fencing tokens, and expired-lease recovery.

The jobs table is the only record of work; there is no in-memory queue. A claim is one conditional
UPDATE, so two workers can never both own a job. Every state change after the claim is fenced by
the per-claim `lease_token`: a worker whose lease expired and was re-claimed cannot complete,
fail, or extend the job. Handlers must publish results idempotently (see analysis_jobs.publish).
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from .context import AppContext
from .db import Job


@dataclass(frozen=True)
class Claim:
    job_id: str
    token: str
    kind: str
    payload: dict
    attempt: int


def enqueue(
    ctx: AppContext, kind: str, payload: dict, idempotency_key: str, contributor_id: str | None = None
) -> Job:
    now = ctx.clock().timestamp()
    try:
        with ctx.sessions() as session, session.begin():
            existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
            if existing is not None:
                return existing
            job = Job(
                kind=kind,
                payload=payload,
                idempotency_key=idempotency_key,
                contributor_id=contributor_id,
                state="queued",
                attempts=0,
                max_attempts=ctx.settings.job_max_attempts,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
        return job
    except IntegrityError:  # a concurrent enqueue with the same key won
        with ctx.sessions() as session:
            return session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))


def _claimable(now: float):
    return or_(Job.state == "queued", and_(Job.state == "running", Job.lease_expires_at <= now))


def claim(ctx: AppContext, worker_id: str) -> Claim | None:
    now = ctx.clock().timestamp()
    lease = ctx.settings.job_lease_s
    with ctx.sessions() as session, session.begin():
        live = session.scalar(
            select(func.count()).select_from(Job).where(Job.state == "running", Job.lease_expires_at > now)
        )
        if live >= ctx.settings.max_concurrent_jobs:
            return None
        candidates = session.scalars(
            select(Job.id).where(_claimable(now)).order_by(Job.created_at).limit(5)
        ).all()
        for job_id in candidates:
            token = str(uuid.uuid4())
            res = session.execute(
                update(Job)
                .where(Job.id == job_id, _claimable(now))
                .values(
                    state="running",
                    lease_owner=worker_id,
                    lease_token=token,
                    lease_expires_at=now + lease,
                    attempts=Job.attempts + 1,
                    updated_at=now,
                )
            )
            if res.rowcount != 1:
                continue
            job = session.get(Job, job_id)
            session.refresh(job)
            if job.attempts > job.max_attempts:
                job.state, job.lease_token, job.lease_owner = "dead", None, None
                job.last_error = (
                    job.last_error or ""
                ) + f"\nabandoned after {job.max_attempts} attempts (lease expiries)"
                continue
            return Claim(job.id, token, job.kind, dict(job.payload), job.attempts)
    return None


def _fenced(session, claim: Claim):
    return session.scalar(
        select(Job).where(Job.id == claim.job_id, Job.lease_token == claim.token, Job.state == "running")
    )


def heartbeat(ctx: AppContext, claim: Claim) -> bool:
    now = ctx.clock().timestamp()
    with ctx.sessions() as session, session.begin():
        res = session.execute(
            update(Job)
            .where(
                Job.id == claim.job_id,
                Job.lease_token == claim.token,
                Job.state == "running",
                Job.lease_expires_at > now,
            )
            .values(lease_expires_at=now + ctx.settings.job_lease_s, updated_at=now)
        )
        return res.rowcount == 1


class LeaseLost(Exception):
    """The claim's lease was taken over by another worker; this worker must not publish."""


def complete_in(session, claim: Claim, result: dict, now: float) -> None:
    """Mark the job succeeded inside the caller's transaction (fenced); raises LeaseLost otherwise."""
    res = session.execute(
        update(Job)
        .where(Job.id == claim.job_id, Job.lease_token == claim.token, Job.state == "running")
        .values(
            state="succeeded",
            result=result,
            lease_token=None,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
    )
    if res.rowcount != 1:
        raise LeaseLost(claim.job_id)


def complete(ctx: AppContext, claim: Claim, result: dict) -> bool:
    try:
        with ctx.sessions() as session, session.begin():
            complete_in(session, claim, result, ctx.clock().timestamp())
    except LeaseLost:
        return False
    return True


def fail(ctx: AppContext, claim: Claim, error: str, retryable: bool = True) -> str | None:
    now = ctx.clock().timestamp()
    with ctx.sessions() as session, session.begin():
        job = _fenced(session, claim)
        if job is None:
            return None
        job.state = "queued" if retryable and job.attempts < job.max_attempts else "dead"
        job.last_error, job.lease_token, job.lease_owner, job.lease_expires_at, job.updated_at = (
            error[-4000:],
            None,
            None,
            None,
            now,
        )
        return job.state


class PermanentJobError(Exception):
    """Deterministic failure (bad input, budget exceeded): do not retry."""


Handler = Callable[[AppContext, Claim], dict]
COMPLETED = "_completed_in_transaction"


def run_once(ctx: AppContext, worker_id: str, handlers: dict[str, Handler]) -> dict | None:
    """Claim and run one job. Handlers that publish call complete_in() in their publication
    transaction and return {COMPLETED: True, ...}; others are completed here."""
    c = claim(ctx, worker_id)
    if c is None:
        return None
    try:
        result = handlers[c.kind](ctx, c)
    except LeaseLost:
        return {"job_id": c.job_id, "state": "lease_lost"}
    except PermanentJobError as exc:
        return {"job_id": c.job_id, "state": fail(ctx, c, f"{exc}", retryable=False)}
    except Exception as exc:  # noqa: BLE001 - recorded on the job, retried
        return {"job_id": c.job_id, "state": fail(ctx, c, "".join(traceback.format_exception(exc)))}
    if result.pop(COMPLETED, False):
        return {"job_id": c.job_id, "state": "succeeded", "result": result}
    ok = complete(ctx, c, result)
    return {"job_id": c.job_id, "state": "succeeded" if ok else "lease_lost", "result": result}
