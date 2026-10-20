"""Stage 1: CSV inputs, component mapping, timing, ambiguity, pre-analysis summary."""

from __future__ import annotations

import gzip

import numpy as np
import pytest

from hvsr_core.io.csv_reader import CSVOptions, read_csv
from hvsr_core.model import InputError, iso_to_ns
from hvsr_core.research.fixture_specs import regenerate


def _csv(rows, header="time_s,N,E,Z"):
    return (header + "\n" + "\n".join(",".join(map(str, r)) for r in rows) + "\n").encode()


@pytest.mark.acceptance("AC-S1-01")
def test_three_layouts_and_gzip(fixture_dir):
    rec, info = read_csv((fixture_dir / "sdof2hz_elapsed_50hz.csv.gz").read_bytes(), CSVOptions(layout="elapsed_time"))
    truth, _ = regenerate("sdof2hz_elapsed_50hz")
    assert rec.sampling_rate_hz == 50.0 and rec.sampling_rate_source == "timestamps"
    assert info.raw_bytes_were_gzip and rec.source["format"] == "csv.gz"
    assert rec.total_samples == 30000 and len(rec.segments) == 1
    assert np.allclose(rec.segments[0].data, truth.segments[0].data, atol=5.1e-7)
    assert rec.t0_utc_ns is None and rec.t0_source == "unknown"

    rec2, _ = read_csv((fixture_dir / "flat_utc_20hz.csv").read_bytes(), CSVOptions(layout="utc_timestamps"))
    t2, _ = regenerate("flat_utc_20hz")
    assert rec2.t0_utc_ns == t2.t0_utc_ns and rec2.t0_source == "data"
    assert rec2.sampling_rate_hz == 20.0 and rec2.total_samples == 1200

    rec3, _ = read_csv(
        (fixture_dir / "flat_samples_only_20hz.csv").read_bytes(),
        CSVOptions(layout="samples_with_rate", sampling_rate_hz=20.0, start_time_utc="2026-10-20T00:00:00Z"),
    )
    assert rec3.sampling_rate_source == "declared" and rec3.t0_utc_ns == iso_to_ns("2026-10-20T00:00:00Z")
    with pytest.raises(InputError, match="explicitly declared sampling rate"):
        read_csv((fixture_dir / "flat_samples_only_20hz.csv").read_bytes(), CSVOptions(layout="samples_with_rate"))


@pytest.mark.acceptance("AC-S1-03")
def test_missing_and_ambiguous_components():
    rows = [(i / 10, 1, 2) for i in range(10)]
    with pytest.raises(InputError) as e:
        read_csv(_csv(rows, "time_s,N,E"), CSVOptions(layout="elapsed_time"))
    assert e.value.code == "missing_component"
    rows4 = [(i / 10, 1, 2, 3, 4) for i in range(10)]
    with pytest.raises(InputError) as e:
        read_csv(_csv(rows4, "time_s,N,HHN,E,Z"), CSVOptions(layout="elapsed_time"))
    assert e.value.code == "ambiguous_component"
    rows3 = [(i / 10, 1, 2, 3) for i in range(10)]
    with pytest.raises(InputError) as e:
        read_csv(_csv(rows3, "time_s,HH1,HH2,HHZ"), CSVOptions(layout="elapsed_time"))
    assert e.value.code == "components_require_mapping"
    rec, _ = read_csv(_csv(rows3, "time_s,HH1,HH2,HHZ"), CSVOptions(layout="elapsed_time", column_map={"N": "HH1", "E": "HH2", "Z": "HHZ"}))
    assert [c.label_kind for c in rec.channels] == ["12Z_mapped", "12Z_mapped", "NEZ"]
    assert any("rotation-invariant" in w for w in rec.diagnostics.warnings)


@pytest.mark.acceptance("AC-S1-05")
def test_gaps_and_irregular_timing_not_resampled():
    t = [i * 0.01 for i in range(100)] + [i * 0.01 for i in range(150, 250)]
    rows = [(f"{x:.2f}", i, -i, 2 * i) for i, x in enumerate(t)]
    rec, _ = read_csv(_csv(rows), CSVOptions(layout="elapsed_time"))
    assert len(rec.segments) == 2 and rec.segments[1].start_index == 150
    assert len(rec.diagnostics.gaps) == 1 and abs(rec.diagnostics.gaps[0].gap_s - 0.5) < 1e-9
    assert rec.total_samples == 200
    assert np.array_equal(rec.segments[1].data[0], np.arange(100, 200, dtype=float)), "values never interpolated"

    jitter = [(f"{i * 0.01 + (0.003 if i % 7 == 0 else 0):.4f}", i, i, i) for i in range(100)]
    rec, _ = read_csv(_csv(jitter), CSVOptions(layout="elapsed_time", sampling_rate_hz=100.0))
    assert rec.diagnostics.timing_irregular and rec.diagnostics.errors
    assert rec.total_samples == 100
    rec, _ = read_csv(_csv(jitter), CSVOptions(layout="elapsed_time", sampling_rate_hz=100.0, timing_policy="nominal_rate", nominal_jitter_tolerance_fraction=0.4))
    assert not rec.diagnostics.timing_irregular and abs(rec.diagnostics.max_timing_jitter_s - 0.003) < 1e-9


@pytest.mark.acceptance("AC-S1-16")
def test_no_silent_inference_of_timezone_units_or_decimal_comma():
    body = "time_utc,N,E,Z\n2026-10-20T00:00:00,1,2,3\n2026-10-20T00:00:01,1,2,3\n"
    with pytest.raises(InputError) as e:
        read_csv(body.encode(), CSVOptions(layout="utc_timestamps"))
    assert e.value.code == "timezone_ambiguous"
    rec, _ = read_csv(body.encode(), CSVOptions(layout="utc_timestamps", timestamps_declared_utc=True))
    assert rec.t0_utc_ns == iso_to_ns("2026-10-20T00:00:00Z")
    mixed = "time_utc,N,E,Z\n2026-10-20T00:00:00Z,1,2,3\n2026-10-20T00:00:01,1,2,3\n"
    with pytest.raises(InputError):
        read_csv(mixed.encode(), CSVOptions(layout="utc_timestamps"))
    offset = "time_utc,N,E,Z\n2026-10-20T00:00:00-05:00,1,2,3\n2026-10-20T00:00:01-05:00,1,2,3\n"
    rec, _ = read_csv(offset.encode(), CSVOptions(layout="utc_timestamps"))
    assert rec.t0_utc_ns == iso_to_ns("2026-10-20T05:00:00Z")
    semi = "time_s;N;E;Z\n0;1,5;2;3\n0.1;1;2;3\n"
    with pytest.raises(InputError) as e:
        read_csv(semi.encode(), CSVOptions(layout="elapsed_time"))
    assert e.value.code == "decimal_comma"
    rec, _ = read_csv(_csv([(0, 1, 2, 3), (0.1, 1, 2, 3)]), CSVOptions(layout="elapsed_time"))
    assert all(c.units is None and c.quantity == "unknown" for c in rec.channels), "units are never guessed"
    rec, _ = read_csv(_csv([(0, 1, 2, 3), (0.1, 1, 2, 3)], "time_s,N [m/s],E [m/s],Z [m/s]"), CSVOptions(layout="elapsed_time"))
    assert {c.units for c in rec.channels} == {"m/s"}, "units explicitly written in the header are recorded"


@pytest.mark.acceptance("AC-S1-19")
def test_pre_analysis_summary(fixture_dir):
    rec, _ = read_csv((fixture_dir / "flat_utc_20hz.csv").read_bytes(), CSVOptions(layout="utc_timestamps"))
    s = rec.summary()
    for key in ("sampling_rate_hz", "components", "start_utc", "end_utc", "duration_s", "sample_count_per_channel", "units", "gaps", "clipped_samples", "timing_irregular", "errors", "warnings"):
        assert key in s
    assert s["duration_s"] == 60.0 and s["sample_count_per_channel"] == 1200
    assert s["start_utc"].endswith("Z")


def test_csv_gz_multi_member_and_bom():
    raw = "\ufefftime_s,N,E,Z\n0,1,2,3\n".encode()
    data = gzip.compress(raw, mtime=0) + gzip.compress(b"0.1,1,2,3\n", mtime=0)
    rec, _ = read_csv(data, CSVOptions(layout="elapsed_time"))
    assert rec.total_samples == 2
