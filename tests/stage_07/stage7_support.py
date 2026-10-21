"""Stage 7 test helpers (a uniquely named module; `conftest` names collide across stage folders)."""

from __future__ import annotations

import datetime as dt
import gzip
from pathlib import Path

from sqlalchemy import select

from hvsr_core.metadata import MeasurementMetadata
from hvsr_web.auth import issue_admin_token, issue_upload_token
from hvsr_web.context import AppContext
from hvsr_web.db import Artifact
from hvsr_web.licensing import ConsentForm
from hvsr_web.submissions import SubmissionRequest, UploadFile, WaveformOptions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SIGNING_KEY = "stage7-test-signing-key-0123456789abcdef"
CSV_GZ = "sdof2hz_elapsed_50hz.csv.gz"
CSV_PLAIN = "flat_utc_20hz.csv"
MSEED = "lognormal3hz_steim2_50hz.mseed"


class Clock:
    def __init__(self):
        self.now = dt.datetime.now(dt.UTC).replace(microsecond=0)

    def __call__(self) -> dt.datetime:
        return self.now

    def epoch(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


def new_contributor(ctx: AppContext, quota_bytes: int | None = None, email: str = "contributor@example.org"):
    with ctx.sessions() as session, session.begin():
        token, c = issue_upload_token(session, private_email=email)
        if quota_bytes is not None:
            c.monthly_quota_bytes = quota_bytes
    return token, c


def new_admin(ctx: AppContext):
    with ctx.sessions() as session, session.begin():
        token, a = issue_admin_token(session, "Test Administrator")
    return token, a


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def csv_gz_logical_bytes() -> int:
    return len(gzip.decompress(fixture_bytes(CSV_GZ)))


def request(
    key: str = "submission-0001",
    *,
    lat: float = 19.4326,
    lon: float = -99.1332,
    consent: bool = True,
    rights: bool = True,
    attribution: str = "Example Observatory",
    **waveform,
) -> SubmissionRequest:
    return SubmissionRequest(
        idempotency_key=key,
        metadata=MeasurementMetadata(latitude=lat, longitude=lon),
        consent=ConsentForm(consent_checked=consent, rights_confirmed=rights, attribution=attribution),
        waveform=WaveformOptions(**(waveform or {"csv_layout": "elapsed_time"})),
    )


def csv_gz_files() -> list[UploadFile]:
    return [UploadFile(CSV_GZ, fixture_bytes(CSV_GZ))]


def artifacts(ctx: AppContext, measurement_id: str, run: bool | None = None) -> list[Artifact]:
    q = select(Artifact).where(Artifact.measurement_id == measurement_id)
    if run is True:
        q = q.where(Artifact.run_id.is_not(None))
    elif run is False:
        q = q.where(Artifact.run_id.is_(None))
    with ctx.sessions() as session:
        return list(session.scalars(q.order_by(Artifact.created_utc, Artifact.id)))


def committed_objects(ctx: AppContext) -> dict[str, bytes]:
    return {i.key: ctx.store.get(i.key) for i in ctx.store.list("") if not i.key.startswith("staging/")}
