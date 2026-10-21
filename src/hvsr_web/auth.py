"""Upload and admin tokens.

Tokens are random 256-bit secrets shown once at issue time and stored only as SHA-256 digests.
They are accepted only in the Authorization header, never in URLs. A token identifies a durable
credential, not a person; no identity claim is derived from IP addresses or cookies.
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from hvsr_core.jsonutil import sha256_bytes

from .db import Admin, Contributor, utc_iso

GUEST_POOL_ID = "00000000-0000-4000-8000-000000000000"
GUEST_POLICY = (
    "Guest uploads share one best-effort, abuse-limited monthly pool. They are not metered per person: "
    "an IP address or browser cookie does not identify a person, so no per-person limit is claimed."
)


class AuthError(PermissionError):
    pass


def token_digest(token: str) -> str:
    return sha256_bytes(token.encode("utf-8"))


def issue_upload_token(
    session: Session, private_email: str | None = None, note: str | None = None
) -> tuple[str, Contributor]:
    token = "hvu_" + secrets.token_urlsafe(32)
    c = Contributor(
        kind="token",
        token_sha256=token_digest(token),
        private_email=private_email,
        private_note=note,
        created_utc=utc_iso(),
    )
    session.add(c)
    session.flush()
    return token, c


def issue_admin_token(session: Session, private_name: str) -> tuple[str, Admin]:
    token = "hva_" + secrets.token_urlsafe(32)
    a = Admin(token_sha256=token_digest(token), private_name=private_name, created_utc=utc_iso())
    session.add(a)
    session.flush()
    return token, a


def contributor_for_token(session: Session, token: str | None) -> Contributor:
    if not token:
        raise AuthError("an upload token is required (Authorization: Bearer <token>)")
    c = session.scalar(select(Contributor).where(Contributor.token_sha256 == token_digest(token)))
    if c is None or not c.active:
        raise AuthError("unknown or inactive upload token")
    return c


def admin_for_token(session: Session, token: str | None) -> Admin:
    if not token:
        raise AuthError("an administrator token is required")
    a = session.scalar(select(Admin).where(Admin.token_sha256 == token_digest(token)))
    if a is None or not a.active:
        raise AuthError("unknown or inactive administrator token")
    return a


def guest_pool(session: Session, enabled: bool) -> Contributor:
    if not enabled:
        raise AuthError("guest uploads are disabled on this deployment; an upload token is required")
    c = session.get(Contributor, GUEST_POOL_ID)
    if c is None:
        c = Contributor(id=GUEST_POOL_ID, kind="guest_pool", private_note=GUEST_POLICY, created_utc=utc_iso())
        session.add(c)
        session.flush()
    return c
