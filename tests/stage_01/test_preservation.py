"""Stage 1: original preservation, canonical conversion and verification, limits."""

from __future__ import annotations

import gzip

import numpy as np
import pytest

from hvsr_core.io.csv_reader import CSVOptions, read_csv
from hvsr_core.io.limits import InputLimits, safe_gunzip
from hvsr_core.io.mseed_reader import read_miniseed
from hvsr_core.io.preservation import (
    build_canonical,
    canonical_only_eligibility,
    finalize_canonical_only,
    preserve_original,
    reconstruct_original,
    reconstructed_csv,
)
from hvsr_core.jsonutil import sha256_bytes
from hvsr_core.model import InputError


class DictStore:
    def __init__(self, fail_on=None, corrupt=None):
        self.d = {}
        self.fail_on = fail_on
        self.corrupt = corrupt

    def put(self, key, data):
        if self.fail_on and key.endswith(self.fail_on):
            raise OSError("simulated storage failure")
        self.d[key] = bytes(data)

    def get(self, key):
        data = self.d[key]
        if self.corrupt and key.endswith(self.corrupt):
            return data[:-1] + bytes([data[-1] ^ 1])
        return data

    def exists(self, key):
        return key in self.d

    def delete(self, key):
        del self.d[key]


def _float_csv(n=500, extra=False, comment=True, negative_zero_row=None):
    rng = np.random.default_rng(7)
    vals = rng.standard_normal((n, 3)) * 1e3
    if negative_zero_row is not None:
        vals[negative_zero_row, 0] = -0.0
    lines = ["# instrument: test logger"] if comment else []
    lines.append("time_s,N,E,Z" + (",temperature_c" if extra else ""))
    for i in range(n):
        a, b, c = (float(v) for v in vals[i])
        row = f"{i / 100:.2f},{a!r},{b!r},{c!r}"
        if extra:
            row += f",{20 + (i % 7) * 0.125:.3f}"
        lines.append(row)
    return ("\n".join(lines) + "\n").encode(), vals


@pytest.mark.acceptance("AC-S1-08")
def test_exact_original_reconstruction(fixture_dir):
    data, _ = _float_csv()
    p = preserve_original("x.csv", data)
    assert p.stored_encoding == "gzip" and p.original_sha256 == sha256_bytes(data)
    assert p.stored_sha256 != p.original_sha256
    assert reconstruct_original(p.stored_bytes, p.stored_encoding, p.original_sha256) == data
    gz = (fixture_dir / "sdof2hz_elapsed_50hz.csv.gz").read_bytes()
    q = preserve_original("y.csv.gz", gz)
    assert q.stored_encoding == "identity" and q.stored_bytes == gz
    assert q.logical_uncompressed_size == len(gzip.decompress(gz)) > len(gz)
    m = (fixture_dir / "lognormal3hz_steim2_50hz.mseed").read_bytes()
    r = preserve_original("z.mseed", m)
    assert r.detected_format == "miniseed" and r.stored_bytes == m and r.stored_sha256 == r.original_sha256


@pytest.mark.acceptance("AC-S1-09")
def test_float64_canonical_roundtrip_bitwise():
    data, vals = _float_csv(negative_zero_row=3)
    assert b"\n0.03,-0.0," in data
    rec, info = read_csv(data, CSVOptions(layout="elapsed_time"))
    art = build_canonical(rec, preserve_original("x.csv", data), info, data)
    assert art.encoding == "FLOAT64" and art.verification["ok"]
    back = read_miniseed([("c.mseed", art.mseed_bytes)], mapping={c["component"]: c["miniseed_id"] for c in art.sidecar["channels"]})
    assert np.array_equal(back.segments[0].data.view(np.uint64), rec.segments[0].data.view(np.uint64))
    assert np.signbit(back.segments[0].data[0, 3]), "-0.0 must survive bit-for-bit"


@pytest.mark.acceptance("AC-S1-10")
def test_integer_steim2_and_no_float_rounding(fixture_dir):
    m = (fixture_dir / "lognormal3hz_steim2_50hz.mseed").read_bytes()
    rec = read_miniseed([("m.mseed", m)])
    art = build_canonical(rec)
    assert art.encoding == "STEIM2" and art.verification["ok"]
    rows = "time_s,N,E,Z\n" + "".join(f"{i / 10},{i * 3},{-i},{i % 5}\n" for i in range(200))
    rec, info = read_csv(rows.encode(), CSVOptions(layout="elapsed_time"))
    assert rec.sample_dtype == "int32"
    assert build_canonical(rec, None, info, rows.encode()).encoding == "STEIM2"
    rows_f = "time_s,N,E,Z\n" + "".join(f"{i / 10},{i * 3}.0,{-i}.0,{i % 5}.0\n" for i in range(200))
    rec, info = read_csv(rows_f.encode(), CSVOptions(layout="elapsed_time"))
    assert rec.sample_dtype == "float64"
    assert build_canonical(rec, None, info, rows_f.encode()).encoding == "FLOAT64", "floats are never converted to integers"


@pytest.mark.acceptance("AC-S1-11")
def test_failed_conversion_never_deletes_source():
    data, _ = _float_csv()
    rec, info = read_csv(data, CSVOptions(layout="elapsed_time"))
    art = build_canonical(rec, preserve_original("x.csv", data), info, data)
    for store in (DictStore(fail_on="canonical.residual.csv.gz"), DictStore(corrupt="canonical.mseed")):
        store.d["staging/orig.csv"] = data
        out = finalize_canonical_only(store, "staging/orig.csv", art, "m/1", consent=True)
        assert not out["ok"] and not out["original_deleted"]
        assert store.d["staging/orig.csv"] == data
    store = DictStore()
    store.d["staging/orig.csv"] = data
    assert not finalize_canonical_only(store, "staging/orig.csv", art, "m/1", consent=False)["ok"]
    assert "staging/orig.csv" in store.d
    out = finalize_canonical_only(store, "staging/orig.csv", art, "m/1", consent=True)
    assert out["ok"] and "staging/orig.csv" not in store.d


@pytest.mark.acceptance("AC-S1-12")
def test_canonical_only_labels_truthful():
    data, vals = _float_csv(extra=True)
    rec, info = read_csv(data, CSVOptions(layout="elapsed_time"))
    art = build_canonical(rec, preserve_original("x.csv", data), info, data)
    store = DictStore()
    store.d["staging/x.csv"] = data
    out = finalize_canonical_only(store, "staging/x.csv", art, "m/2", consent=True)
    assert out["original_bytes_retained"] is False
    recon = reconstructed_csv(store.get(out["keys"]["mseed"]), art.sidecar, store.get(out["keys"]["residual"]))
    first = recon.decode().splitlines()[0]
    assert "Reconstructed CSV" in first and "Original CSV" not in recon.decode()
    assert recon != data, "reconstruction is not claimed to be byte-identical"


@pytest.mark.acceptance("AC-S1-13")
def test_decompression_bomb_rejected():
    bomb = gzip.compress(b"\x00" * (64 * 1024 * 1024), compresslevel=9)
    with pytest.raises(InputError) as e:
        safe_gunzip(bomb, InputLimits())
    assert e.value.code == "decompression_ratio_limit"
    with pytest.raises(InputError) as e:
        safe_gunzip(gzip.compress(b"1,2,3\n" * 100000), InputLimits(max_decoded_bytes=100_000))
    assert e.value.code == "decoded_size_limit"
    with pytest.raises(InputError):
        read_csv(bomb, CSVOptions(layout="elapsed_time"))


@pytest.mark.acceptance("AC-S1-14")
def test_nonrepresentable_timestamps():
    jitter = "time_s,N,E,Z\n" + "".join(f"{i * 0.01 + (0.004 if i % 3 == 0 else 0):.4f},{i}.5,{i}.25,{i}.0\n" for i in range(300))
    rec, info = read_csv(jitter.encode(), CSVOptions(layout="elapsed_time", sampling_rate_hz=100.0))
    ok, reasons = canonical_only_eligibility(rec, info, None)
    assert not ok and any("irregular timing" in r for r in reasons)
    sub_us = "time_utc,N,E,Z\n" + "".join(f"2026-10-20T00:00:{i // 100:02d}.{i % 100:02d}0000123Z,{i}.5,1.5,2.5\n" for i in range(300))
    rec, info = read_csv(sub_us.encode(), CSVOptions(layout="utc_timestamps"))
    art = build_canonical(rec, preserve_original("t.csv", sub_us.encode()), info, sub_us.encode())
    assert art.sidecar["start_time"]["miniseed_start_exact"] is False
    assert art.sidecar["start_time"]["exact_utc_ns"] == rec.t0_utc_ns and rec.t0_utc_ns % 1000 == 123
    ok, reasons = canonical_only_eligibility(rec, info, art)
    assert ok, reasons
    residual = gzip.decompress(art.residual_gz).decode()
    assert "2026-10-20T00:00:00.000000123Z" in residual, "original timestamp text preserved exactly"


@pytest.mark.acceptance("AC-S1-15")
def test_extra_columns_preserved():
    data, _ = _float_csv(extra=True)
    rec, info = read_csv(data, CSVOptions(layout="elapsed_time"))
    assert info.extra_columns == ["temperature_c"]
    art = build_canonical(rec, preserve_original("x.csv", data), info, data)
    assert art.sidecar["residual_columns"]["columns"] == ["time_s", "temperature_c"]
    recon = reconstructed_csv(art.mseed_bytes, art.sidecar, art.residual_gz).decode().splitlines()
    original = data.decode().splitlines()
    assert recon[1] == "# instrument: test logger"
    for o, r in zip(original[2:50], recon[3:51], strict=True):
        assert o.split(",")[4] == r.split(",")[4], "extra column text identical"
        assert o.split(",")[0] == r.split(",")[0], "time text identical"
        assert float(o.split(",")[1]) == float(r.split(",")[1])
