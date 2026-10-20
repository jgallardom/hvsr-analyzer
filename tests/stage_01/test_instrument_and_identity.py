"""Stage 1: instrument/response status, StationXML, record identity, photos."""

from __future__ import annotations

import io
from dataclasses import replace

import numpy as np
import pytest
from obspy import UTCDateTime
from obspy.core.inventory import Channel, Inventory, Network, Response, Station
from obspy.core.inventory.response import InstrumentSensitivity
from PIL import Image

from hvsr_core.identity import decimal_year, geohash_code_part, geohash_encode, record_code, with_suffix
from hvsr_core.io.stationxml import apply_stationxml, parse_stationxml
from hvsr_core.metadata import ChannelSensor, SensorMetadata, apply_sensor_metadata, evaluate_instrument
from hvsr_core.model import ChannelMeta, InputError, Recording, Segment, iso_to_ns


def _rec(**chan_kw) -> Recording:
    chans = tuple(ChannelMeta(c, f"XX.STA..HH{c}", **chan_kw.get(c, {})) for c in "NEZ")
    return Recording(100.0, iso_to_ns("2026-10-20T00:00:00Z"), "data", chans, [Segment(0, 0.0, np.zeros((3, 10)))])


@pytest.mark.acceptance("AC-S1-04")
def test_gain_and_quantity_mismatch():
    r = _rec(N={"sensitivity": 1e9}, E={"sensitivity": 1e9}, Z={"sensitivity": 2e9})
    out = evaluate_instrument(r, None, "none")
    assert out["status"] == "invalid" and "gain_mismatch" in out["flags"]
    r = _rec(**{c: {"sensitivity": s, "quantity": "counts", "sensed_quantity": "velocity"} for c, s in zip("NEZ", (1e9, 1e9, 2e9), strict=True)})
    out = evaluate_instrument(r, None, "scalar_sensitivity")
    assert out["status"] == "verified_common_quantity"
    assert out["scale"] == {"N": 1e-9, "E": 1e-9, "Z": 0.5e-9}
    r = _rec(N={"sensed_quantity": "velocity"}, E={"sensed_quantity": "velocity"}, Z={"sensed_quantity": "acceleration"})
    assert evaluate_instrument(r, None, "none")["status"] == "invalid"
    assert evaluate_instrument(r, None, "scalar_sensitivity")["status"] == "invalid"
    r = _rec(N={"units": "m/s"}, E={"units": "m/s"}, Z={"units": "counts"})
    out = evaluate_instrument(r, None, "none")
    assert out["status"] == "invalid" and "unit_mismatch" in out["flags"]
    r = _rec(N={"sensitivity": 1e9}, E={}, Z={"sensitivity": 1e9})
    out = evaluate_instrument(r, None, "scalar_sensitivity")
    assert out["status"] == "invalid" and "missing_sensitivity" in out["flags"]


@pytest.mark.acceptance("AC-S1-07")
def test_unknown_response_is_provisional():
    out = evaluate_instrument(_rec(), None, "none")
    assert out["status"] == "provisional_uncalibrated"
    assert "instrument_response_not_verified" in out["flags"]
    sensor = SensorMetadata(channels=[ChannelSensor(component=c, units="m/s", quantity="velocity", sensed_quantity="velocity") for c in "NEZ"])
    r = apply_sensor_metadata(_rec(), sensor)
    assert evaluate_instrument(r, sensor, "none")["status"] == "verified_common_quantity"


def _stationxml(sens=(1.5e9, 1.5e9, 3.0e9)) -> bytes:
    chans = []
    for c, s in zip("NEZ", sens, strict=True):
        resp = Response(instrument_sensitivity=InstrumentSensitivity(value=s, frequency=1.0, input_units="M/S", output_units="COUNTS"))
        chans.append(Channel(code=f"HH{c}", location_code="", latitude=0, longitude=0, elevation=0, depth=0, azimuth=0, dip=0, sample_rate=100, response=resp, start_date=UTCDateTime(2020, 1, 1)))
    inv = Inventory(networks=[Network(code="XX", stations=[Station(code="STA", latitude=0, longitude=0, elevation=0, channels=chans, creation_date=UTCDateTime(2020, 1, 1))])], source="test")
    buf = io.BytesIO()
    inv.write(buf, format="STATIONXML")
    return buf.getvalue()


@pytest.mark.acceptance("AC-S1-21")
def test_stationxml_sensitivity_and_checksum():
    data = _stationxml()
    inv, sha = parse_stationxml(data)
    assert len(sha) == 64
    rec, report = apply_stationxml(_rec(), inv, sha)
    assert [c.sensitivity for c in rec.channels] == [1.5e9, 1.5e9, 3.0e9]
    assert {c.sensed_quantity for c in rec.channels} == {"velocity"}
    assert report["stationxml_sha256"] == sha and report["channels"]["Z"]["status"] == "matched"
    out = evaluate_instrument(rec, SensorMetadata(stationxml_sha256=sha), "scalar_sensitivity")
    assert out["status"] == "verified_common_quantity" and out["correction"] == "scalar_sensitivity"
    with pytest.raises(InputError) as e:
        parse_stationxml(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><FDSNStationXML/>')
    assert e.value.code == "stationxml_unsafe"


@pytest.mark.acceptance("AC-S1-17")
def test_record_identity():
    gh = geohash_encode(57.64911, 10.40744, 12)
    assert gh.startswith("u4pruydqqvj")  # standard reference example
    assert len(gh) == 12 and geohash_code_part(gh) == "pruyd"
    assert decimal_year(iso_to_ns("2024-07-02T00:00:00Z")) == 2024.5  # 183/366 days, leap year
    assert decimal_year(iso_to_ns("2023-07-02T12:00:00Z")) == 2023.5  # 182.5/365 days
    assert decimal_year(iso_to_ns("2026-01-01T00:00:00Z")) == 2026.0
    code = record_code(9.0, -79.5, iso_to_ns("2026-10-20T12:00:00Z"))
    assert code["base_code"] == f"{code['geohash'][2:7]}-{code['decimal_year']:.4f}"
    assert code["decimal_year"] == 2026 + 292.5 / 365  # Jan..Sep = 273 days, +19 days of Oct, +0.5 day
    assert code["base_code"].endswith("-2026.8014")
    assert with_suffix("abcde-2026.8014", 1) == "abcde-2026.8014"
    assert with_suffix("abcde-2026.8014", 3) == "abcde-2026.8014-03"


@pytest.mark.acceptance("AC-S1-18")
def test_photo_processing_strips_exif_and_pads():
    from hvsr_web.photos import process_photo

    img = Image.new("RGB", (640, 320), (200, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6  # orientation: rotate 90 CW on display
    exif[0x8825] = {1: "N", 2: (9.0, 0.0, 0.0)}  # GPS IFD
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    out, info = process_photo(buf.getvalue())
    with Image.open(io.BytesIO(out)) as res:
        assert res.size == (300, 300)
        assert not res.getexif(), "EXIF (including GPS) must be stripped"
        px = res.convert("RGB")
        # orientation applied: content becomes portrait -> white pads left/right
        assert px.getpixel((5, 150))[1] > 240 and px.getpixel((150, 150))[0] > 150
    assert info["input_size"] == [640, 320]
    with pytest.raises(InputError):
        process_photo(b"not an image")
    big = Image.new("L", (5000, 5000))
    b2 = io.BytesIO()
    big.save(b2, format="PNG")
    with pytest.raises(InputError) as e:
        process_photo(b2.getvalue(), max_pixels=10_000_000)
    assert e.value.code == "photo_pixels"
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, format="GIF")
    with pytest.raises(InputError):
        process_photo(gif.getvalue())
    _ = replace
