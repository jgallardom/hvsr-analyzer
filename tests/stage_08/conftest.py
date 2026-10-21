"""Stage 8 fixtures. The shared site is read-mostly; tests that depend on on-request state use `fresh`."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from stage8_support import (
    CSV_GZ,
    Site,
    analyze,
    fixture_bytes,
    get_json,
    make_site,
    png_photo,
    submit,
    synthetic_csv_gz,
)


@dataclass
class Published:
    measurement_id: str
    record_code: str
    run_id: str
    run_b_id: str | None
    lat: float
    lon: float


@pytest.fixture(scope="session")
def site(tmp_path_factory) -> Site:
    return make_site(tmp_path_factory.mktemp("stage8-site"))


def _publish(
    site: Site, files, key: str, lat: float, lon: float, body: dict | None = None, **kw
) -> Published:
    sub = submit(site, files, key, lat=lat, lon=lon, **kw)
    run_id = analyze(site, sub["measurement_id"], body)
    run = get_json(site, f"/api/v1/runs/{run_id}")
    manifest = get_json(site, run["manifest_url"])
    b = [r["run_id"] for r in manifest["provenance"]["analysis_runs"] if r["primary_run_id"] == run_id]
    return Published(sub["measurement_id"], sub["record_code"], run_id, b[0] if b else None, lat, lon)


@pytest.fixture(scope="session")
def short_run(site) -> Published:
    """600 s fixture recording, default request (Standard + SDOF, SESAME, planned 1.5 x Lw run B).
    Too short for an estimable band (fewer informative blocks than the guard requires)."""
    files = [(CSV_GZ, fixture_bytes(CSV_GZ), "waveform"), ("sensor.png", png_photo(), "photo_sensor")]
    return _publish(
        site, files, "stage8-short-0001", 19.4326, -99.1332, metadata={"ground_surface": "compacted soil"}
    )


@pytest.fixture(scope="session")
def long_run(site) -> Published:
    """1800 s synthetic SDOF recording (fn 2 Hz): long enough for the nominal 95% band."""
    files = [("synthetic_1800s.csv.gz", synthetic_csv_gz(1800, seed=21), "waveform")]
    return _publish(site, files, "stage8-long-0001", 40.416775, -3.70379, {"robustness_check": False})


@pytest.fixture
def fresh(tmp_path) -> Site:
    return make_site(tmp_path)
