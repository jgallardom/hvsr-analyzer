"""Stage 1: MiniSEED inputs, supported encodings, synchronization."""

from __future__ import annotations

import io

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime

from hvsr_core.io.mseed_reader import SUPPORTED_ENCODINGS, read_miniseed
from hvsr_core.model import InputError

T0 = UTCDateTime("2026-10-20T00:00:00Z")


def _trace(comp, data, fs=100.0, start=T0, sta="STA01", loc="", band="HH"):
    tr = Trace(data=data)
    tr.stats.network, tr.stats.station, tr.stats.location, tr.stats.channel = "XX", sta, loc, f"{band}{comp}"
    tr.stats.sampling_rate = fs
    tr.stats.starttime = start
    return tr


def _write(traces, encoding="STEIM2") -> bytes:
    buf = io.BytesIO()
    Stream(traces).write(buf, format="MSEED", encoding=encoding, reclen=512)
    return buf.getvalue()


def _data(enc, n=1000, seed=0):
    rng = np.random.default_rng(seed)
    if enc == "INT16":
        return rng.integers(-2000, 2000, n).astype(np.int16)
    if enc in ("INT32", "STEIM1", "STEIM2"):
        return rng.integers(-200000, 200000, n).astype(np.int32)
    if enc == "FLOAT32":
        return rng.standard_normal(n).astype(np.float32)
    return rng.standard_normal(n)


@pytest.mark.acceptance("AC-S1-02")
@pytest.mark.parametrize("enc", SUPPORTED_ENCODINGS)
def test_supported_encodings_roundtrip(enc):
    arrays = {c: _data(enc, seed=i) for i, c in enumerate("NEZ")}
    payload = _write([_trace(c, arrays[c]) for c in "NEZ"], encoding=enc)
    rec = read_miniseed([("x.mseed", payload)])
    assert rec.sampling_rate_hz == 100.0 and rec.t0_utc_ns == T0.ns
    for i, c in enumerate("NEZ"):
        assert np.array_equal(rec.segments[0].data[i], arrays[c].astype(np.float64))
    assert rec.sample_dtype == ("float64" if enc.startswith("FLOAT") else "int32")


@pytest.mark.acceptance("AC-S1-02")
def test_separate_files_and_explicit_multi_trace_mapping():
    files = [(f"{c}.mseed", _write([_trace(c, _data("STEIM2", seed=i))])) for i, c in enumerate("NEZ")]
    rec = read_miniseed(files)
    assert [c.source_label for c in rec.channels] == ["XX.STA01..HHN", "XX.STA01..HHE", "XX.STA01..HHZ"]
    two_sta = [
        _trace(c, _data("STEIM2", seed=i), sta=s) for s in ("STA01", "STA02") for i, c in enumerate("NEZ")
    ]
    payload = _write(two_sta)
    with pytest.raises(InputError) as e:
        read_miniseed([("x.mseed", payload)])
    assert e.value.code == "channel_mapping_required"
    rec = read_miniseed(
        [("x.mseed", payload)], mapping={"N": "XX.STA02..HHN", "E": "XX.STA02..HHE", "Z": "XX.STA02..HHZ"}
    )
    assert rec.channels[0].source_label == "XX.STA02..HHN"


@pytest.mark.acceptance("AC-S1-03")
def test_mseed_12z_requires_mapping():
    payload = _write([_trace(c, _data("STEIM2", seed=i)) for i, c in enumerate("12Z")])
    with pytest.raises(InputError) as e:
        read_miniseed([("x.mseed", payload)])
    assert e.value.code == "components_require_mapping"
    rec = read_miniseed(
        [("x.mseed", payload)], mapping={"N": "XX.STA01..HH1", "E": "XX.STA01..HH2", "Z": "XX.STA01..HHZ"}
    )
    assert rec.channels[0].label_kind == "12Z_mapped"
    payload2 = _write([_trace(c, _data("STEIM2", seed=i)) for i, c in enumerate("NE")])
    with pytest.raises(InputError):
        read_miniseed([("x.mseed", payload2)])


@pytest.mark.acceptance("AC-S1-05")
def test_mseed_gap_detected_not_filled():
    a = _data("STEIM2", 1000)
    traces = []
    for i, c in enumerate("NEZ"):
        traces.append(_trace(c, a[:500].copy()))
        traces.append(_trace(c, a[500:].copy(), start=T0 + 5.0 + 2.0))
    rec = read_miniseed([("x.mseed", _write(traces))])
    assert len(rec.segments) == 2 and rec.segments[1].start_index == 700
    assert abs(rec.diagnostics.gaps[0].gap_s - 2.0) < 1e-9
    assert rec.total_samples == 1000


@pytest.mark.acceptance("AC-S1-06")
def test_unsupported_versions_and_encodings():
    ms3 = b"MS\x03" + b"\x00" * 100
    with pytest.raises(InputError) as e:
        read_miniseed([("x.ms3", ms3)])
    assert e.value.code == "unsupported_miniseed_version"
    tr = Trace(np.frombuffer(b"abc" * 100, dtype="|S1"))
    tr.stats.channel = "HHN"
    buf = io.BytesIO()
    Stream([tr]).write(buf, format="MSEED", encoding="ASCII")
    with pytest.raises(InputError) as e:
        read_miniseed([("x.mseed", buf.getvalue())])
    assert e.value.code == "unsupported_encoding"
    with pytest.raises(InputError) as e:
        read_miniseed([("x.bin", b"\x01\x02garbage" * 50)])
    assert e.value.code == "not_miniseed"


@pytest.mark.acceptance("AC-S1-20")
def test_channels_not_silently_combined():
    traces = [
        _trace("N", _data("STEIM2")),
        _trace("E", _data("STEIM2")),
        _trace("Z", _data("STEIM2", 2000), fs=200.0),
    ]
    with pytest.raises(InputError) as e:
        read_miniseed([("x.mseed", _write(traces))])
    assert e.value.code == "sampling_rate_mismatch"
    traces = [
        _trace("N", _data("STEIM2")),
        _trace("E", _data("STEIM2")),
        _trace("Z", _data("STEIM2"), start=T0 + 0.005),
    ]
    with pytest.raises(InputError) as e:
        read_miniseed([("x.mseed", _write(traces))])
    assert e.value.code == "channels_not_synchronized"
    traces = [
        _trace("N", _data("STEIM2")),
        _trace("E", _data("STEIM2")),
        _trace("Z", _data("STEIM2"), start=T0 + 0.5),
    ]
    rec = read_miniseed([("x.mseed", _write(traces))])
    assert rec.total_samples == 950, "only the common, sample-aligned span is used"
