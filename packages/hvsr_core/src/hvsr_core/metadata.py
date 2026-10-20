"""Measurement-context and sensor/recorder metadata (spec 2.2) plus instrument status checks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .model import Recording

Level = Literal["unknown", "none", "low", "moderate", "high"]
Context = Literal[
    "free_field_ground",
    "building_ground_floor",
    "building_upper_floor",
    "basement",
    "structure_other",
    "unknown",
]
QuantityField = Literal["counts", "velocity", "acceleration", "displacement", "other", "unknown"]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelSensor(_M):
    component: Literal["N", "E", "Z"]
    source_label: str | None = None
    azimuth_deg: float | None = Field(None, ge=0, lt=360)
    dip_deg: float | None = Field(None, ge=-90, le=90)
    quantity: QuantityField = "unknown"
    units: str | None = Field(None, description="Physical units of the recorded samples, e.g. counts, m/s, m/s**2.")
    sensitivity: float | None = Field(None, gt=0, description="Counts per physical unit (overall channel gain).")
    sensitivity_units: str | None = None
    full_scale: float | None = Field(None, gt=0, description="Digitizer full scale (in sample units) for clipping checks.")


class SensorMetadata(_M):
    manufacturer: str | None = None
    model: str | None = None
    instrument_id: str | None = None
    sensor_type: str | None = Field(None, description="e.g. short-period velocimeter, broadband, accelerometer, geophone.")
    digitizer_gain: float | None = None
    response_epoch: str | None = None
    natural_frequency_hz: float | None = Field(None, gt=0, description="Nominal natural/corner frequency (not a validity limit).")
    usable_fmin_hz: float | None = Field(None, gt=0, description="Documented usable band lower limit, if known.")
    usable_fmax_hz: float | None = Field(None, gt=0, description="Documented usable band upper limit, if known.")
    usable_band_source: str | None = None
    calibration_reference: str | None = None
    channels: list[ChannelSensor] = Field(default_factory=list)
    stationxml_sha256: str | None = None
    instrument_correction_applied_before_upload: bool | None = None
    instrument_correction_description: str | None = None


class MeasurementMetadata(_M):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    wind: Level = "unknown"
    traffic: Level = "unknown"
    ground_surface: str | None = Field(None, description="Surface material, e.g. soil, grass, asphalt, concrete.")
    measurement_context: Context = "unknown"
    floor_or_height: str | None = None
    coupling_notes: str | None = None
    observations: str | None = None
    site_group_id: str | None = None
    sensor: SensorMetadata = Field(default_factory=SensorMetadata)


InstrumentStatus = Literal["verified_common_quantity", "provisional_uncalibrated", "invalid"]


def evaluate_instrument(recording: Recording, sensor: SensorMetadata | None, correction: str) -> dict:
    """Decide whether the H/V ratio of this recording is physically meaningful.

    Returns a dict with ``status`` (verified_common_quantity | provisional_uncalibrated | invalid),
    precise ``reasons``, ``flags``, the correction actually applicable, per-channel scale factors to
    apply (``scale``), and the declared usable band.
    """
    reasons: list[str] = []
    flags: list[str] = []
    chans = {c.component: c for c in recording.channels}
    quantities = {k: c.quantity for k, c in chans.items()}
    units = {k: c.units for k, c in chans.items()}
    sens = {k: c.sensitivity for k, c in chans.items()}
    scale = {"N": 1.0, "E": 1.0, "Z": 1.0}

    known_q = {q for q in quantities.values() if q not in ("unknown", "counts")}
    if len(known_q) > 1:
        return {
            "status": "invalid",
            "reasons": [f"channels record physically different quantities {quantities}; H/V would divide incompatible quantities"],
            "flags": ["incompatible_quantities"],
            "correction": "none",
            "scale": scale,
            "usable_band_hz": _band(sensor),
        }

    if correction == "scalar_sensitivity":
        missing = [k for k, s in sens.items() if s is None]
        if missing:
            return {
                "status": "invalid",
                "reasons": [f"scalar_sensitivity correction requested but sensitivity missing for {missing}"],
                "flags": ["missing_sensitivity"],
                "correction": "none",
                "scale": scale,
                "usable_band_hz": _band(sensor),
            }
        for k in scale:
            scale[k] = 1.0 / float(sens[k])  # type: ignore[arg-type]
        status: InstrumentStatus = "verified_common_quantity"
        reasons.append("each channel divided by its declared overall sensitivity (valid only within the passband)")
        if any(q == "unknown" for q in quantities.values()):
            status = "provisional_uncalibrated"
            flags.append("instrument_response_not_verified")
            reasons.append("physical quantity of at least one channel is unknown")
        return {"status": status, "reasons": reasons, "flags": flags, "correction": "scalar_sensitivity", "scale": scale, "usable_band_hz": _band(sensor)}

    # No correction requested.
    distinct_units = {u for u in units.values() if u is not None}
    if len(distinct_units) > 1:
        return {
            "status": "invalid",
            "reasons": [f"channels have different units {units} and no correction to a common quantity was requested"],
            "flags": ["unit_mismatch"],
            "correction": "none",
            "scale": scale,
            "usable_band_hz": _band(sensor),
        }
    known_sens = {k: s for k, s in sens.items() if s is not None}
    if len(known_sens) == 3 and len({round(s, 12) for s in known_sens.values()}) > 1:
        return {
            "status": "invalid",
            "reasons": [f"channel sensitivities differ {known_sens}; request scalar_sensitivity correction"],
            "flags": ["gain_mismatch"],
            "correction": "none",
            "scale": scale,
            "usable_band_hz": _band(sensor),
        }
    if 0 < len(known_sens) < 3:
        flags.append("instrument_response_not_verified")
        reasons.append("sensitivity known for only some channels; equal gains cannot be verified")
        return {"status": "provisional_uncalibrated", "reasons": reasons, "flags": flags, "correction": "none", "scale": scale, "usable_band_hz": _band(sensor)}
    if len(known_sens) == 3:
        reasons.append("all channels share the same declared sensitivity and units; gains cancel in H/V")
        if any(q == "unknown" for q in quantities.values()):
            flags.append("instrument_response_not_verified")
            reasons.append("recorded physical quantity unknown")
            return {"status": "provisional_uncalibrated", "reasons": reasons, "flags": flags, "correction": "none", "scale": scale, "usable_band_hz": _band(sensor)}
        return {"status": "verified_common_quantity", "reasons": reasons, "flags": flags, "correction": "none", "scale": scale, "usable_band_hz": _band(sensor)}
    flags.append("instrument_response_not_verified")
    reasons.append("no channel sensitivities declared; equal channel gains assumed but not verified")
    return {"status": "provisional_uncalibrated", "reasons": reasons, "flags": flags, "correction": "none", "scale": scale, "usable_band_hz": _band(sensor)}


def _band(sensor: SensorMetadata | None) -> list[float | None]:
    if sensor is None:
        return [None, None]
    return [sensor.usable_fmin_hz, sensor.usable_fmax_hz]


def apply_sensor_metadata(recording: Recording, sensor: SensorMetadata | None) -> Recording:
    """Return a recording whose ChannelMeta carries declared sensor fields (explicit values win)."""
    if sensor is None or not sensor.channels:
        return recording
    from dataclasses import replace

    by_comp = {c.component: c for c in sensor.channels}
    new = []
    for ch in recording.channels:
        s = by_comp.get(ch.component)
        if s is None:
            new.append(ch)
            continue
        new.append(
            replace(
                ch,
                azimuth_deg=s.azimuth_deg if s.azimuth_deg is not None else ch.azimuth_deg,
                dip_deg=s.dip_deg if s.dip_deg is not None else ch.dip_deg,
                quantity=s.quantity if s.quantity != "unknown" else ch.quantity,
                units=s.units if s.units is not None else ch.units,
                sensitivity=s.sensitivity if s.sensitivity is not None else ch.sensitivity,
                sensitivity_units=s.sensitivity_units if s.sensitivity_units is not None else ch.sensitivity_units,
                full_scale=s.full_scale if s.full_scale is not None else ch.full_scale,
            )
        )
    recording.channels = tuple(new)  # type: ignore[assignment]
    return recording
