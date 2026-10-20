"""Measurement-context and sensor/recorder metadata (spec 2.2) plus instrument status checks."""

from __future__ import annotations

from dataclasses import replace
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
RecordedQuantity = Literal["counts", "velocity", "acceleration", "displacement", "other", "unknown"]
SensedQuantity = Literal["velocity", "acceleration", "displacement", "other", "unknown"]
PHYSICAL = ("velocity", "acceleration", "displacement")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelSensor(_M):
    component: Literal["N", "E", "Z"]
    source_label: str | None = None
    azimuth_deg: float | None = Field(None, ge=0, lt=360)
    dip_deg: float | None = Field(None, ge=-90, le=90)
    quantity: RecordedQuantity = Field("unknown", description="Quantity of the recorded samples (counts if uncorrected).")
    sensed_quantity: SensedQuantity = Field("unknown", description="Physical quantity the sensor responds to.")
    units: str | None = Field(None, description="Units of the recorded samples, e.g. counts, m/s, m/s**2.")
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


def _result(status: str, reasons: list[str], flags: list[str], correction: str, scale: dict, sensor) -> dict:
    return {
        "status": status,
        "reasons": reasons,
        "flags": flags,
        "correction": correction,
        "scale": scale,
        "usable_band_hz": [sensor.usable_fmin_hz, sensor.usable_fmax_hz] if sensor is not None else [None, None],
        "natural_frequency_hz": sensor.natural_frequency_hz if sensor is not None else None,
    }


def evaluate_instrument(recording: Recording, sensor: SensorMetadata | None, correction: str) -> dict:
    """Decide whether H/V of this recording divides comparable quantities.

    status: verified_common_quantity | provisional_uncalibrated | invalid, with precise reasons.
    ``scale`` holds per-component multipliers applied by the preprocessing step.
    """
    ch = {c.component: c for c in recording.channels}
    scale = {"N": 1.0, "E": 1.0, "Z": 1.0}
    sensed = {k: c.sensed_quantity for k, c in ch.items()}
    recorded = {k: c.quantity for k, c in ch.items()}
    units = {k: c.units for k, c in ch.items()}
    sens = {k: c.sensitivity for k, c in ch.items()}

    known_sensed = {q for q in sensed.values() if q != "unknown"}
    if len(known_sensed) > 1:
        return _result("invalid", [f"sensors respond to different physical quantities {sensed}; H/V would divide incompatible quantities"], ["incompatible_quantities"], "none", scale, sensor)
    known_recorded_phys = {q for q in recorded.values() if q in PHYSICAL}
    if len(known_recorded_phys) > 1:
        return _result("invalid", [f"recorded samples are in different physical quantities {recorded}"], ["incompatible_quantities"], "none", scale, sensor)

    if correction == "scalar_sensitivity":
        missing = [k for k, s in sens.items() if s is None]
        if missing:
            return _result("invalid", [f"scalar_sensitivity correction requested but sensitivity missing for {missing}"], ["missing_sensitivity"], "none", scale, sensor)
        if any(recorded[k] in PHYSICAL for k in ch):
            return _result("invalid", ["scalar_sensitivity correction requested for samples already in physical units"], ["double_correction"], "none", scale, sensor)
        for k in scale:
            scale[k] = 1.0 / float(sens[k])  # type: ignore[arg-type]
        reasons = ["each channel divided by its declared overall sensitivity (valid only within the instrument passband)"]
        if len(known_sensed) == 1 and all(q != "unknown" for q in sensed.values()):
            return _result("verified_common_quantity", reasons, [], "scalar_sensitivity", scale, sensor)
        reasons.append("the physical quantity sensed by at least one channel is unknown")
        return _result("provisional_uncalibrated", reasons, ["instrument_response_not_verified"], "scalar_sensitivity", scale, sensor)

    distinct_units = {u for u in units.values() if u is not None}
    if len(distinct_units) > 1:
        return _result("invalid", [f"channels have different units {units} and no correction to a common quantity was requested"], ["unit_mismatch"], "none", scale, sensor)
    known_sens = {k: s for k, s in sens.items() if s is not None}
    if len(known_sens) == 3 and len({float(s) for s in known_sens.values()}) > 1:
        return _result("invalid", [f"channel sensitivities differ {known_sens}; request scalar_sensitivity correction"], ["gain_mismatch"], "none", scale, sensor)
    if len(known_recorded_phys) == 1 and all(q in PHYSICAL for q in recorded.values()) and len(distinct_units) == 1 and None not in units.values():
        return _result("verified_common_quantity", ["samples are declared in a common physical quantity and unit"], [], "none", scale, sensor)
    if len(known_sens) == 3 and len(known_sensed) == 1 and all(q != "unknown" for q in sensed.values()):
        return _result("verified_common_quantity", ["all channels share the same declared sensitivity and sensed quantity; gains cancel in H/V"], [], "none", scale, sensor)
    if len(known_sens) == 3:
        return _result("provisional_uncalibrated", ["equal declared sensitivities, but the sensed physical quantity is unknown"], ["instrument_response_not_verified"], "none", scale, sensor)
    if known_sens:
        return _result("provisional_uncalibrated", ["sensitivity known for only some channels; equal gains cannot be verified"], ["instrument_response_not_verified"], "none", scale, sensor)
    return _result("provisional_uncalibrated", ["no channel sensitivities declared; equal channel gains assumed but not verified"], ["instrument_response_not_verified"], "none", scale, sensor)


def apply_sensor_metadata(recording: Recording, sensor: SensorMetadata | None) -> Recording:
    """Attach declared per-channel sensor fields to the recording (explicit declared values win)."""
    if sensor is None or not sensor.channels:
        return recording
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
                sensed_quantity=s.sensed_quantity if s.sensed_quantity != "unknown" else ch.sensed_quantity,
                units=s.units if s.units is not None else ch.units,
                sensitivity=s.sensitivity if s.sensitivity is not None else ch.sensitivity,
                sensitivity_units=s.sensitivity_units if s.sensitivity_units is not None else ch.sensitivity_units,
                full_scale=s.full_scale if s.full_scale is not None else ch.full_scale,
            )
        )
    recording.channels = tuple(new)  # type: ignore[assignment]
    return recording
