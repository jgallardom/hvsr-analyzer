#!/usr/bin/env python3
"""Write the stored synthetic fixtures and tests/fixtures/manifest.json."""

from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

import numpy as np
from obspy import Stream, Trace, UTCDateTime

from hvsr_core.jsonutil import sha256_bytes
from hvsr_core.model import ns_to_iso
from hvsr_core.research.fixture_specs import FIXTURE_LICENSE, FIXTURES, regenerate

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def csv_elapsed(rec) -> bytes:
    data = rec.segments[0].data
    t = np.arange(data.shape[1]) / rec.sampling_rate_hz
    buf = io.StringIO()
    buf.write("time_s,N,E,Z\n")
    for i in range(data.shape[1]):
        buf.write(f"{t[i]:.4f},{data[0, i]:.6f},{data[1, i]:.6f},{data[2, i]:.6f}\n")
    return buf.getvalue().encode()


def csv_utc(rec) -> bytes:
    data = rec.segments[0].data
    buf = io.StringIO()
    buf.write("time_utc,N,E,Z\n")
    step_ns = round(1e9 / rec.sampling_rate_hz)
    for i in range(data.shape[1]):
        ts = ns_to_iso(rec.t0_utc_ns + i * step_ns)
        buf.write(f"{ts},{data[0, i]:.6f},{data[1, i]:.6f},{data[2, i]:.6f}\n")
    return buf.getvalue().encode()


def csv_samples(rec) -> bytes:
    data = rec.segments[0].data
    buf = io.StringIO()
    buf.write("N,E,Z\n")
    for i in range(data.shape[1]):
        buf.write(f"{data[0, i]:.6f},{data[1, i]:.6f},{data[2, i]:.6f}\n")
    return buf.getvalue().encode()


def mseed_steim2(rec) -> bytes:
    st = Stream()
    start = UTCDateTime(ns=rec.t0_utc_ns)
    for ci, comp in enumerate("NEZ"):
        tr = Trace(data=rec.segments[0].data[ci].astype(np.int32))
        tr.stats.network, tr.stats.station, tr.stats.location, tr.stats.channel = (
            "XX",
            "SYN01",
            "",
            f"HH{comp}",
        )
        tr.stats.sampling_rate = rec.sampling_rate_hz
        tr.stats.starttime = start
        st.append(tr)
    buf = io.BytesIO()
    st.write(buf, format="MSEED", encoding="STEIM2", reclen=4096)
    return buf.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {"license": FIXTURE_LICENSE, "fixtures": {}}
    for name, spec in FIXTURES.items():
        rec, truth = regenerate(name)
        fmt = spec["format"]
        if fmt == "csv_elapsed_time":
            raw = csv_elapsed(rec)
            payload = gzip.compress(raw, mtime=0)
        elif fmt == "csv_utc_timestamps":
            payload = csv_utc(rec)
        elif fmt == "csv_samples_with_rate":
            payload = csv_samples(rec)
        elif fmt == "miniseed2_steim2":
            payload = mseed_steim2(rec)
        else:
            raise ValueError(fmt)
        (OUT / spec["file"]).write_bytes(payload)
        manifest["fixtures"][name] = {
            "file": spec["file"],
            "format": fmt,
            "sha256": sha256_bytes(payload),
            "byte_size": len(payload),
            "description": spec["description"],
            "license": FIXTURE_LICENSE,
            "known_properties": {
                "sampling_rate_hz": rec.sampling_rate_hz,
                "samples_per_channel": rec.total_samples,
                "start_utc": ns_to_iso(rec.t0_utc_ns),
                "transfer": truth["tf"],
                "tf_peak_frequency_hz": truth["tf_peak_frequency_hz"],
                "tf_peak_amplitude": truth["tf_peak_amplitude"],
                "generator": truth["generator"],
                "seed": truth["seed"],
            },
        }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: v["byte_size"] for k, v in manifest["fixtures"].items()}, indent=1))


if __name__ == "__main__":
    main()
