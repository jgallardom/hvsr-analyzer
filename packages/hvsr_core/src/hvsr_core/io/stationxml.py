"""Optional StationXML: checksum, safe parsing, per-channel overall sensitivity."""

from __future__ import annotations

import io
from dataclasses import replace

from ..jsonutil import sha256_bytes
from ..model import InputError, Recording

_UNIT_QUANTITY = {
    "M/S": "velocity",
    "M/SEC": "velocity",
    "M/S**2": "acceleration",
    "M/S2": "acceleration",
    "M/SEC**2": "acceleration",
    "M": "displacement",
    "COUNTS": "counts",
    "COUNT": "counts",
}


def parse_stationxml(data: bytes, max_bytes: int = 20 * 1024 * 1024):
    if len(data) > max_bytes:
        raise InputError("stationxml_size", "StationXML exceeds the size limit")
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise InputError("stationxml_unsafe", "StationXML with DOCTYPE/ENTITY declarations is rejected")
    from obspy import read_inventory

    try:
        inv = read_inventory(io.BytesIO(data), format="STATIONXML")
    except Exception as exc:  # noqa: BLE001
        raise InputError(
            "stationxml_invalid", f"unreadable StationXML ({type(exc).__name__}: {exc})"
        ) from exc
    return inv, sha256_bytes(data)


def apply_stationxml(recording: Recording, inventory, sha256: str) -> tuple[Recording, dict]:
    """Attach overall sensitivity / input units from StationXML to each channel (SEED ids required)."""
    report = {"stationxml_sha256": sha256, "channels": {}}
    new = []
    when = None
    if recording.t0_utc_ns is not None:
        from obspy import UTCDateTime

        when = UTCDateTime(ns=recording.t0_utc_ns)
    for ch in recording.channels:
        sid = ch.source_label
        parts = sid.split(".")
        if len(parts) != 4:
            report["channels"][ch.component] = {
                "status": "not_matched",
                "reason": "channel has no NET.STA.LOC.CHA identifier",
            }
            new.append(ch)
            continue
        net, sta, loc, cha = parts
        sel = inventory.select(network=net, station=sta, location=loc, channel=cha, time=when)
        chans = [c for n in sel for s in n for c in s]
        if len(chans) != 1 or chans[0].response is None or chans[0].response.instrument_sensitivity is None:
            report["channels"][ch.component] = {
                "status": "not_matched",
                "reason": f"{len(chans)} matching channel epochs with sensitivity",
            }
            new.append(ch)
            continue
        c = chans[0]
        sens = c.response.instrument_sensitivity
        in_units = (sens.input_units or "").upper()
        quantity = _UNIT_QUANTITY.get(in_units, "other")
        new.append(
            replace(
                ch,
                sensitivity=float(sens.value),
                sensitivity_units=f"{sens.output_units}/{sens.input_units}",
                quantity="counts",
                units="counts",
                sensed_quantity=quantity
                if quantity in ("velocity", "acceleration", "displacement")
                else "other",
                azimuth_deg=c.azimuth if c.azimuth is not None else ch.azimuth_deg,
                dip_deg=c.dip if c.dip is not None else ch.dip_deg,
            )
        )
        report["channels"][ch.component] = {
            "status": "matched",
            "sensitivity": float(sens.value),
            "input_units": sens.input_units,
            "output_units": sens.output_units,
            "physical_quantity_after_correction": quantity,
            "frequency_hz": sens.frequency,
        }
    recording.channels = tuple(new)  # type: ignore[assignment]
    return recording, report
