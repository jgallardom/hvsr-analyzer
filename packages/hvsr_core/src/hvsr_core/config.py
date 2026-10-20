"""Analysis configuration model.

Every parameter that can change a scientific number lives in :class:`AnalysisConfig` and is part of
its scientific hash. Reporting (SESAME), display, and robustness-planning options live in
:class:`AnalysisRequest` and are excluded from that hash (spec 12.1).

All defaults are initial, editable research/operational settings, not universally suitable values
(spec 5). Each field carries a description; ``docs/configuration.md`` is generated from this module
and a test checks that it is in sync.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .jsonutil import canonical_hash
from .versions import SCHEMA_VERSIONS


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class BandpassFilter(_Model):
    low_hz: float | None = Field(None, gt=0, description="High-pass corner in Hz (None = no high-pass).")
    high_hz: float | None = Field(None, gt=0, description="Low-pass corner in Hz (None = no low-pass).")
    order: int = Field(4, ge=1, le=8, description="Butterworth order per pass.")
    zero_phase: bool = Field(True, description="Apply forward-backward (sosfiltfilt) zero-phase filtering.")

    @model_validator(mode="after")
    def _check(self) -> BandpassFilter:
        if self.low_hz is None and self.high_hz is None:
            raise ValueError("bandpass filter requires low_hz and/or high_hz")
        if self.low_hz is not None and self.high_hz is not None and self.low_hz >= self.high_hz:
            raise ValueError("low_hz must be < high_hz")
        return self


class Preprocessing(_Model):
    detrend: Literal["none", "constant", "linear"] = Field(
        "linear", description="Per-window detrend applied after the per-window mean removal."
    )
    taper_type: Literal["tukey"] = Field("tukey", description="Per-window taper family.")
    taper_alpha: float = Field(
        0.1, ge=0.0, le=1.0, description="Tukey taper fraction (total tapered fraction of the window)."
    )
    bandpass: BandpassFilter | None = Field(
        None, description="Optional record-level Butterworth filter. Off by default; recorded as a derived operation."
    )
    response_correction: Literal["none", "scalar_sensitivity"] = Field(
        "none",
        description=(
            "Instrument correction. 'scalar_sensitivity' divides each channel by its declared overall "
            "sensitivity (valid only inside the declared passband). Full deconvolution is not implemented."
        ),
    )


class TimeRange(_Model):
    tmin_s: float | None = Field(
        None, ge=0, description="Analyzed start, seconds after the first canonical sample (None = start)."
    )
    tmax_s: float | None = Field(
        None, gt=0, description="Analyzed end, seconds after the first canonical sample (None = end)."
    )

    @model_validator(mode="after")
    def _check(self) -> TimeRange:
        if self.tmin_s is not None and self.tmax_s is not None and self.tmax_s <= self.tmin_s:
            raise ValueError("tmax_s must be > tmin_s")
        return self


class Windowing(_Model):
    window_length_s: float = Field(
        50.0, gt=0, description="Window length Lw in seconds; rounded to an integer sample count (actual value recorded)."
    )
    step_s: float = Field(
        5.0, gt=0, description="Candidate window step dt in seconds; rounded to an integer sample count."
    )
    time_range: TimeRange = Field(default_factory=TimeRange, description="Analyzed time span.")


class Spectral(_Model):
    horizontal_combination: Literal["rms", "geometric"] = Field(
        "rms", description="rms: H = sqrt((N^2 + E^2)/2); geometric: H = sqrt(N*E)."
    )
    processing_order: Literal["combine_then_smooth"] = Field(
        "combine_then_smooth",
        description="Horizontal combination of raw Fourier amplitudes, then Konno-Ohmachi smoothing of H and V (hvsrpy 2.x convention).",
    )
    smoothing: Literal["konno_ohmachi"] = Field("konno_ohmachi", description="Smoothing operator.")
    ko_bandwidth: float = Field(40.0, gt=0, description="Konno-Ohmachi bandwidth coefficient b.")
    ko_support_rad: float = Field(
        3.0,
        gt=0,
        le=math.pi,
        description="Konno-Ohmachi truncation: weights used where |b*log10(f/fc)| <= this value (3.0 matches hvsrpy).",
    )
    grid_points_per_decade: int = Field(
        200, ge=20, le=2000, description="Anchored log grid f_i = 10**(i/P); P points per decade."
    )
    grid_fmin_hz: float | None = Field(
        None, gt=0, description="Lowest grid frequency (None = peak_search.fmin_hz / 4, for SESAME [f0/4, 4 f0] checks)."
    )
    grid_fmax_hz: float | None = Field(
        None, gt=0, description="Highest grid frequency (None = peak_search.fmax_hz * 4, capped by the anti-alias limit)."
    )
    anti_alias_fraction: float = Field(
        0.8, gt=0, le=1.0, description="Frequencies above this fraction of Nyquist are invalid (anti-alias roll-off)."
    )
    min_smoothing_bins: int = Field(
        3, ge=1, description="Minimum nonzero-frequency FFT bins inside the smoothing support for a valid grid point."
    )
    vertical_floor_rel: float = Field(
        1e-9,
        gt=0,
        description=(
            "A smoothed vertical amplitude below this fraction of the window's median smoothed vertical "
            "amplitude makes the bin invalid (no invented H/V)."
        ),
    )


class PeakSearch(_Model):
    fmin_hz: float = Field(0.2, gt=0, description="Lower limit of the peak search band (Hz).")
    fmax_hz: float = Field(20.0, gt=0, description="Upper limit of the peak search band (Hz).")

    @model_validator(mode="after")
    def _check(self) -> PeakSearch:
        if self.fmax_hz <= self.fmin_hz:
            raise ValueError("fmax_hz must be > fmin_hz")
        return self


class BasicQC(_Model):
    clip_detection: bool = Field(True, description="Reject windows containing clipped samples.")
    clip_fraction_of_full_scale: float = Field(
        0.995, gt=0, le=1, description="|x| >= this fraction of a declared full-scale value counts as clipped."
    )
    flat_top_run_samples: int = Field(
        5, ge=2, description="Runs of >= this many identical samples at the record extreme count as clipped."
    )
    sta_lta_enabled: bool = Field(True, description="Anti-trigger transient screening with STA/LTA on |x|.")
    sta_s: float = Field(1.0, gt=0, description="Short-term average length (s).")
    lta_s: float = Field(30.0, gt=0, description="Long-term average length (s).")
    sta_lta_min: float = Field(0.2, ge=0, description="Window rejected if STA/LTA falls below this anywhere inside it.")
    sta_lta_max: float = Field(2.5, gt=0, description="Window rejected if STA/LTA exceeds this anywhere inside it.")


class StandardSelection(_Model):
    frequency_outlier_rejection: Literal["none", "fdwra_cox2020"] = Field(
        "none",
        description="Optional frequency-domain window rejection after basic QC (can alter the peak distribution).",
    )
    fdwra_n_std: float = Field(2.0, gt=0, description="FDWRA rejection half-width in lognormal standard deviations.")
    fdwra_max_iterations: int = Field(50, ge=1, description="FDWRA iteration cap.")


class PeakDiscovery(_Model):
    window_min_prominence_ratio: float = Field(
        1.25, gt=1, description="Per-window local peak minimum topographic prominence (amplitude ratio)."
    )
    kde_bandwidth_decades: float = Field(
        0.03, gt=0, description="Gaussian KDE bandwidth (log10 decades) for clustering window peaks into families."
    )
    family_max_halfwidth_decades: float = Field(
        0.12, gt=0, description="Maximum family half-width around its KDE mode (log10 decades)."
    )
    kde_merge_valley_ratio: float = Field(
        0.8,
        gt=0,
        le=1,
        description="Adjacent KDE modes merge when the density minimum between them is >= this fraction of the lower mode.",
    )
    min_recurrence: float = Field(
        0.3, gt=0, le=1, description="Minimum fraction of discovery windows with a peak inside the family."
    )
    family_listing_min_recurrence: float = Field(
        0.1, ge=0, le=1, description="Families below this recurrence are not listed at all (clutter control only)."
    )
    mean_min_prominence_ratio: float = Field(
        1.5, gt=1, description="Discovery mean-curve local peak minimum prominence (amplitude ratio) for plausibility."
    )
    min_discovery_windows: int = Field(5, ge=2, description="Minimum non-overlapping QC windows for discovery.")
    lower_edge_min_hv: float = Field(
        1.5, gt=0, description="Rising curve at the lowest usable frequency with H/V >= this flags lower_band_unresolved."
    )
    narrow_line_min_contrast_ratio: float = Field(
        3.0, gt=1, description="Raw-spectrum line contrast vs. its neighbourhood that counts as a narrow line."
    )
    narrow_line_max_width_bins: int = Field(3, ge=1, description="Maximum width (FFT bins) of a narrow line.")
    narrow_line_min_persistence: float = Field(
        0.8, gt=0, le=1, description="Fraction of windows in which the line must recur to be persistent."
    )
    smoothing_sensitivity_bandwidth: float = Field(
        80.0, gt=0, description="Alternate (narrower) Konno-Ohmachi b used for the smoothing-sensitivity diagnostic."
    )


class SDOFMatching(_Model):
    template_bank: str = Field(
        "auto",
        description="SDOF bank ID; 'auto' selects the shipped bank conditioned for this run's Konno-Ohmachi b if available.",
    )
    extra_banks: tuple[str, ...] = Field(
        (), description="Optional additional template families (e.g. 2DOF/3DOF bank IDs), reported separately."
    )
    peak_interpolation: Literal["parabolic_log", "none"] = Field(
        "parabolic_log",
        description="Peak estimator: 3-point parabola in (ln f, ln A), offset bounded to +/- half a grid step.",
    )
    min_fft_bins_in_fit_band: int = Field(
        10, ge=3, description="Minimum original FFT bins inside [fp/2, 2 fp] for a rankable fit."
    )
    min_fft_bins_below_peak: int = Field(4, ge=1, description="Minimum original FFT bins inside [fp/2, fp).")
    min_log_variance: float = Field(
        1e-6, gt=0, description="Weighted variance of y below this makes R-squared undefined (unrankable)."
    )
    r2_min: float = Field(
        0.0, description="Eligibility requires R2_log strictly greater than this (0 = better than a constant)."
    )
    calibration_profile: str | None = Field(
        None, description="Optional offline calibration profile ID supplying stronger eligibility thresholds."
    )
    selection_mode: Literal["automatic", "manual_top_n"] = Field(
        "automatic", description="automatic: all greedy survivors; manual_top_n: first n (selection-conditional)."
    )
    manual_n: int | None = Field(None, ge=1, description="Requested count for manual_top_n.")

    @model_validator(mode="after")
    def _check(self) -> SDOFMatching:
        if self.selection_mode == "manual_top_n" and self.manual_n is None:
            raise ValueError("manual_top_n requires manual_n")
        if self.selection_mode == "automatic" and self.manual_n is not None:
            raise ValueError("manual_n is only valid with selection_mode='manual_top_n'")
        return self


class Uncertainty(_Model):
    n_bootstrap: int = Field(2000, ge=100, le=20000, description="Bootstrap replicates for final results.")
    seed: int = Field(20261020, ge=0, description="Deterministic bootstrap seed (recorded).")
    min_informative_blocks: int = Field(
        10, ge=2, description="Experimental guard: fewer informative blocks returns ci_not_estimable."
    )
    regular_spacing_max_ratio: float = Field(
        1.5, ge=1, description="Retained sequence is 'regular' if max/min spacing between consecutive windows <= this."
    )
    irregular_block_lw_factor: float = Field(
        5.0, gt=0, description="Irregular sequences: original-time block length = factor * Lw (before dependence doubling)."
    )
    sensitivity_factors: tuple[float, ...] = Field(
        (0.5, 2.0), description="Predeclared block-length multipliers reported as sensitivity (never chosen by width)."
    )
    adjacent_block_corr_max: float = Field(
        0.3, gt=0, lt=1, description="If lag-1 correlation of block-mean features exceeds this, block length is doubled."
    )
    max_block_doublings: int = Field(3, ge=0, description="Maximum dependence-driven block doublings.")
    sensitivity_width_ratio_limit: float = Field(
        1.5, gt=1, description="Band width ratio across sensitivity lengths above this marks ci_limited."
    )


class AnalysisConfig(_Model):
    """All scientific parameters. Its hash identifies a scientific computation."""

    config_schema_version: Literal["2.0.0"] = Field(SCHEMA_VERSIONS["config"], description="Configuration schema version.")
    preprocessing: Preprocessing = Field(default_factory=Preprocessing, description="Preprocessing settings.")
    windowing: Windowing = Field(default_factory=Windowing, description="Window length, step, and time span.")
    spectral: Spectral = Field(default_factory=Spectral, description="Spectral estimation and grid settings.")
    peak_search: PeakSearch = Field(default_factory=PeakSearch, description="Peak search band.")
    basic_qc: BasicQC = Field(default_factory=BasicQC, description="Basic invalid-data checks shared by all methods.")
    standard_selection: StandardSelection = Field(
        default_factory=StandardSelection, description="Standard reference selection options."
    )
    peak_discovery: PeakDiscovery = Field(default_factory=PeakDiscovery, description="Shape-neutral peak-family discovery.")
    sdof: SDOFMatching = Field(default_factory=SDOFMatching, description="Peak-aligned template matching and selection.")
    uncertainty: Uncertainty = Field(default_factory=Uncertainty, description="Dependence-aware bootstrap settings.")

    def scientific_hash(self) -> str:
        return canonical_hash({"config": self.model_dump(mode="json")})


class ReportOptions(_Model):
    evaluate_sesame: bool = Field(
        True, description="Evaluate SESAME criteria (reporting only; never changes scientific results)."
    )


class DisplayOptions(_Model):
    show_individual_windows: bool = Field(False, description="Overlay selected individual window curves.")
    show_alternate_method: bool = Field(False, description="Overlay the other method's mean curve.")
    figure_width_in: float = Field(9.0, gt=2, le=30, description="Static figure width (inches).")
    figure_height_in: float = Field(6.0, gt=2, le=30, description="Static figure height (inches).")
    dpi: int = Field(150, ge=50, le=600, description="PNG resolution.")


class AnalysisRequest(_Model):
    config: AnalysisConfig = Field(default_factory=AnalysisConfig, description="Scientific configuration.")
    report: ReportOptions = Field(default_factory=ReportOptions, description="Reporting options.")
    display: DisplayOptions = Field(default_factory=DisplayOptions, description="Display options.")
    robustness_check: bool = Field(
        True, description="Plan run B (1.5 x Lw, other settings unchanged) before results are examined."
    )
    robustness_lw_factor: Literal[1.5] = Field(1.5, description="Run B window-length multiplier (fixed in v1).")


def robustness_config(config: AnalysisConfig, fs_hz: float) -> AnalysisConfig:
    """Run B: Lw * 1.5 rounded to a valid integer sample count; everything else unchanged."""
    n_a = round(config.windowing.window_length_s * fs_hz)
    n_b = round(1.5 * n_a)
    windowing = config.windowing.model_copy(update={"window_length_s": n_b / fs_hz})
    return config.model_copy(update={"windowing": windowing})
