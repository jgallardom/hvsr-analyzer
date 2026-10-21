"""Stage 9 test helpers (a uniquely named module; `conftest` names collide across stage folders)."""

from __future__ import annotations

import gzip
import io
import uuid
from pathlib import Path

from hvsr_core.metadata import MeasurementMetadata
from hvsr_core.research.synthetic import TFSpec, generate_3c
from hvsr_web.auth import issue_admin_token, issue_upload_token
from hvsr_web.context import AppContext
from hvsr_web.licensing import ConsentForm
from hvsr_web.settings import Settings
from hvsr_web.submissions import SubmissionRequest, UploadFile, WaveformOptions, submit

SIGNING_KEY = "stage9-test-signing-key-0123456789abcdef"


def make_ctx(tmp_path: Path, name: str = "main", **overrides) -> AppContext:
    values = {
        "database_url": f"sqlite:///{tmp_path / name}.sqlite3",
        "local_store_dir": str(tmp_path / f"{name}-objects"),
        "bank_dir": str(tmp_path / f"{name}-banks"),
        "url_signing_key": SIGNING_KEY,
    }
    return AppContext.from_settings(Settings(**(values | overrides)))


def fresh_postgres_db(server_url: str) -> str:
    """Create an empty database on the test server; returns its URL in the hosted `postgresql://` form."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from hvsr_web.db import driver_url

    name = "t_" + uuid.uuid4().hex[:16]
    engine = create_engine(driver_url(server_url), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    return make_url(server_url).set(database=name).render_as_string(hide_password=False)


def migrate_after(url: str, barrier) -> str:
    """Process entry point: wait for every process, then migrate (concurrent service startup)."""
    from hvsr_web.db import migrate

    barrier.wait(timeout=120)
    return migrate(url)


def synthetic_csv_gz(duration_s: float, seed: int, fs: float = 50.0, fn: float = 2.0) -> bytes:
    """A synthetic three-component SDOF-response recording as an elapsed-time CSV."""
    rec, _ = generate_3c(duration_s=duration_s, fs=fs, tf=TFSpec("sdof", {"fn": fn, "zeta": 0.2}), seed=seed)
    data = rec.segments[0].data
    lines = ["time_s,N,E,Z\n"]
    for i in range(data.shape[1]):
        lines.append(f"{i / fs:.3f},{data[0, i]:.6f},{data[1, i]:.6f},{data[2, i]:.6f}\n")
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write("".join(lines).encode())
    return buf.getvalue()


def stationxml_bytes() -> bytes:
    """A valid StationXML document (one station, HHN/HHE/HHZ with overall sensitivities)."""
    from obspy import UTCDateTime
    from obspy.core.inventory import (
        Channel,
        InstrumentSensitivity,
        Inventory,
        Network,
        Response,
        Station,
    )

    chans = [
        Channel(
            code=f"HH{c}", location_code="", latitude=0, longitude=0, elevation=0, depth=0, azimuth=0,
            dip=0, sample_rate=100, start_date=UTCDateTime(2020, 1, 1),
            response=Response(instrument_sensitivity=InstrumentSensitivity(
                value=1.5e9, frequency=1.0, input_units="M/S", output_units="COUNTS")),
        )
        for c in "NEZ"
    ]  # fmt: skip
    sta = Station(code="STA", latitude=0, longitude=0, elevation=0, channels=chans)
    inv = Inventory(networks=[Network(code="XX", stations=[sta])], source="stage9")
    buf = io.BytesIO()
    inv.write(buf, format="STATIONXML")
    return buf.getvalue()


def new_contributor(ctx: AppContext):
    with ctx.sessions() as session, session.begin():
        token, c = issue_upload_token(session, private_email="stage9@example.org")
    return token, c


def new_admin(ctx: AppContext, name: str = "Stage 9 Administrator"):
    with ctx.sessions() as session, session.begin():
        token, a = issue_admin_token(session, name)
    return token, a


def submission(key: str) -> SubmissionRequest:
    return SubmissionRequest(
        idempotency_key=key,
        metadata=MeasurementMetadata(latitude=40.4168, longitude=-3.7038),
        consent=ConsentForm(
            consent_checked=True, rights_confirmed=True, attribution="Stage 9 Test Observatory"
        ),
        waveform=WaveformOptions(csv_layout="elapsed_time"),
    )


def submit_synthetic(ctx: AppContext, contributor, duration_s: float, seed: int, fs: float = 50.0):
    data = synthetic_csv_gz(duration_s, seed, fs)
    return submit(
        ctx,
        contributor,
        submission(f"s9-{seed}-{int(duration_s)}-{int(fs)}"),
        [UploadFile("rec.csv.gz", data)],
    )
