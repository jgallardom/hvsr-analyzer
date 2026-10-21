"""Object-store adapters: local filesystem and S3-compatible (Cloudflare R2, Backblaze B2, AWS S3).

One bucket, separate logical categories (spec 13.2). Committed objects are content-addressed and
immutable: writing different bytes to an existing committed key is refused. Only `staging/` keys
are mutable and deletable by routine cleanup.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from hvsr_core.jsonutil import sha256_bytes

CATEGORIES = ("recordings", "metadata", "analyses", "plots", "templates", "photos")
STAGING_PREFIX = "staging/"
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class ImmutableObjectError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    modified_epoch_s: float


def blob_key(category: str, sha256: str) -> str:
    if category not in CATEGORIES:
        raise ValueError(f"unknown object category {category!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("blob keys are addressed by a lowercase hex SHA-256")
    return f"{category}/sha256/{sha256[:2]}/{sha256}"


def check_key(key: str) -> str:
    if not _KEY_RE.match(key) or ".." in key.split("/") or "//" in key:
        raise ValueError(f"invalid object key {key!r}")
    return key


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def list(self, prefix: str = "") -> list[ObjectInfo]: ...
    def signed_url(
        self, key: str, ttl_s: int, filename: str | None = None, content_type: str | None = None
    ) -> str: ...


class LocalObjectStore:
    """Filesystem store with atomic writes. Signed URLs point at the app's /api/v1/blobs endpoint."""

    def __init__(self, root: str | Path, signing_key: bytes | None = None, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._key = signing_key or secrets.token_bytes(32)
        self._clock = clock

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self._path(key)
        if p.exists() and not key.startswith(STAGING_PREFIX):
            if sha256_bytes(p.read_bytes()) == sha256_bytes(data):
                return
            raise ImmutableObjectError(f"refusing to overwrite committed object {key}")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.is_file():
            raise KeyError(key)
        return p.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.name.startswith(".tmp-"):
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    st = p.stat()
                    out.append(ObjectInfo(key, st.st_size, st.st_mtime))
        return out

    def _sig(self, key: str, exp: int, filename: str | None, content_type: str | None) -> str:
        msg = f"{key}\n{exp}\n{filename or ''}\n{content_type or ''}".encode()
        return (
            base64.urlsafe_b64encode(hmac.new(self._key, msg, hashlib.sha256).digest()).decode().rstrip("=")
        )

    def signed_url(
        self, key: str, ttl_s: int, filename: str | None = None, content_type: str | None = None
    ) -> str:
        exp = int(self._clock()) + int(ttl_s)
        q = f"exp={exp}&sig={self._sig(check_key(key), exp, filename, content_type)}"
        if filename:
            q += f"&filename={quote(filename)}"
        if content_type:
            q += f"&type={quote(content_type)}"
        return f"/api/v1/blobs/{key}?{q}"

    def verify_signature(
        self, key: str, exp: int, sig: str, filename: str | None = None, content_type: str | None = None
    ) -> bool:
        if int(exp) < self._clock():
            return False
        return hmac.compare_digest(self._sig(key, int(exp), filename, content_type), sig)


class S3ObjectStore:
    """S3 API adapter. Credentials come from the standard AWS_* environment variables or IAM."""

    def __init__(self, bucket: str, client=None, endpoint_url: str | None = None, region: str | None = None):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3", endpoint_url=endpoint_url, region_name=region, config=Config(signature_version="s3v4")
            )
        self.client = client
        self.bucket = bucket

    @staticmethod
    def _code(exc) -> str:
        return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        from botocore.exceptions import ClientError

        check_key(key)
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        kw = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "ChecksumSHA256": digest,
        }
        if key.startswith(STAGING_PREFIX):
            self.client.put_object(**kw)
            return
        try:
            self.client.put_object(**kw, IfNoneMatch="*")
        except ClientError as exc:
            if self._code(exc) not in ("PreconditionFailed", "412", "ConditionalRequestConflict"):
                raise
            if sha256_bytes(self.get(key)) != sha256_bytes(data):
                raise ImmutableObjectError(f"refusing to overwrite committed object {key}") from exc

    def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self.client.get_object(Bucket=self.bucket, Key=check_key(key))["Body"].read()
        except ClientError as exc:
            if self._code(exc) in ("NoSuchKey", "404"):
                raise KeyError(key) from exc
            raise

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=check_key(key))
            return True
        except ClientError as exc:
            if self._code(exc) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=check_key(key))

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        out = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                m = o["LastModified"]
                ts = m.timestamp() if isinstance(m, dt.datetime) else float(m)
                out.append(ObjectInfo(o["Key"], int(o["Size"]), ts))
        return out

    def signed_url(
        self, key: str, ttl_s: int, filename: str | None = None, content_type: str | None = None
    ) -> str:
        params = {"Bucket": self.bucket, "Key": check_key(key)}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        if content_type:
            params["ResponseContentType"] = content_type
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=int(ttl_s))


def make_store(settings) -> ObjectStore:
    if settings.object_store == "s3":
        if not settings.s3_bucket:
            raise ValueError("HVSR_S3_BUCKET is required when HVSR_OBJECT_STORE=s3")
        return S3ObjectStore(
            settings.s3_bucket, endpoint_url=settings.s3_endpoint_url, region=settings.s3_region
        )
    key = settings.url_signing_key.get_secret_value().encode() if settings.url_signing_key else None
    return LocalObjectStore(settings.local_store_dir, key)
