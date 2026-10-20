# Configuration reference

Generated from `hvsr_core.config` by `uv run hvsr docs`. Do not edit by hand.

Defaults are initial, editable settings, not universally suitable values. Fields under
`config.` are scientific and enter the scientific configuration hash. `report.`, `display.`, and
`robustness_*` do not.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `config` | `AnalysisConfig` | `(see nested fields)` | Scientific configuration. |
| `config.config_schema_version` | `one of '2.0.0'` | `'2.0.0'` | Configuration schema version. |
| `config.preprocessing` | `Preprocessing` | `(see nested fields)` | Preprocessing settings. |
| `config.preprocessing.detrend` | `one of 'none', 'constant', 'linear'` | `'linear'` | Per-window detrend applied after the per-window mean removal. |
| `config.preprocessing.taper_type` | `one of 'tukey'` | `'tukey'` | Per-window taper family. |
| `config.preprocessing.taper_alpha` | `float` | `0.1` | Tukey taper fraction (total tapered fraction of the window). |
| `config.preprocessing.bandpass` | `BandpassFilter \| None` | `None` | Optional record-level Butterworth filter. Off by default; recorded as a derived operation. |
| `config.preprocessing.bandpass.low_hz` | `float \| None` | `None` | High-pass corner in Hz (None = no high-pass). |
| `config.preprocessing.bandpass.high_hz` | `float \| None` | `None` | Low-pass corner in Hz (None = no low-pass). |
| `config.preprocessing.bandpass.order` | `int` | `4` | Butterworth order per pass. |
| `config.preprocessing.bandpass.zero_phase` | `bool` | `True` | Apply forward-backward (sosfiltfilt) zero-phase filtering. |
| `config.preprocessing.response_correction` | `one of 'none', 'scalar_sensitivity'` | `'none'` | Instrument correction. 'scalar_sensitivity' divides each channel by its declared overall sensitivity (valid only inside the declared passband). Full deconvolution is not implemented. |
| `config.windowing` | `Windowing` | `(see nested fields)` | Window length, step, and time span. |
| `config.windowing.window_length_s` | `float` | `50.0` | Window length Lw in seconds; rounded to an integer sample count (actual value recorded). |
| `config.windowing.step_s` | `float` | `5.0` | Candidate window step dt in seconds; rounded to an integer sample count. |
| `config.windowing.time_range` | `TimeRange` | `(see nested fields)` | Analyzed time span. |
| `config.windowing.time_range.tmin_s` | `float \| None` | `None` | Analyzed start, seconds after the first canonical sample (None = start). |
| `config.windowing.time_range.tmax_s` | `float \| None` | `None` | Analyzed end, seconds after the first canonical sample (None = end). |
| `config.spectral` | `Spectral` | `(see nested fields)` | Spectral estimation and grid settings. |
| `config.spectral.horizontal_combination` | `one of 'rms', 'geometric'` | `'rms'` | rms: H = sqrt((N^2 + E^2)/2); geometric: H = sqrt(N*E). |
| `config.spectral.processing_order` | `one of 'combine_then_smooth'` | `'combine_then_smooth'` | Horizontal combination of raw Fourier amplitudes, then Konno-Ohmachi smoothing of H and V (hvsrpy 2.x convention). |
| `config.spectral.smoothing` | `one of 'konno_ohmachi'` | `'konno_ohmachi'` | Smoothing operator. |
| `config.spectral.ko_bandwidth` | `float` | `40.0` | Konno-Ohmachi bandwidth coefficient b. |
| `config.spectral.ko_support_rad` | `float` | `3.0` | Konno-Ohmachi truncation: weights used where \|b*log10(f/fc)\| <= this value (3.0 matches hvsrpy). |
| `config.spectral.grid_points_per_decade` | `int` | `200` | Anchored log grid f_i = 10**(i/P); P points per decade. |
| `config.spectral.grid_fmin_hz` | `float \| None` | `None` | Lowest grid frequency (None = peak_search.fmin_hz / 4, for SESAME [f0/4, 4 f0] checks). |
| `config.spectral.grid_fmax_hz` | `float \| None` | `None` | Highest grid frequency (None = peak_search.fmax_hz * 4, capped by the anti-alias limit). |
| `config.spectral.anti_alias_fraction` | `float` | `0.8` | Frequencies above this fraction of Nyquist are invalid (anti-alias roll-off). |
| `config.spectral.min_smoothing_bins` | `int` | `3` | Minimum nonzero-frequency FFT bins inside the smoothing support for a valid grid point. |
| `config.spectral.vertical_floor_rel` | `float` | `1e-09` | A smoothed vertical amplitude below this fraction of the window's median smoothed vertical amplitude makes the bin invalid (no invented H/V). |
| `config.peak_search` | `PeakSearch` | `(see nested fields)` | Peak search band. |
| `config.peak_search.fmin_hz` | `float` | `0.2` | Lower limit of the peak search band (Hz). |
| `config.peak_search.fmax_hz` | `float` | `20.0` | Upper limit of the peak search band (Hz). |
| `config.basic_qc` | `BasicQC` | `(see nested fields)` | Basic invalid-data checks shared by all methods. |
| `config.basic_qc.clip_detection` | `bool` | `True` | Reject windows containing clipped samples. |
| `config.basic_qc.clip_fraction_of_full_scale` | `float` | `0.995` | \|x\| >= this fraction of a declared full-scale value counts as clipped. |
| `config.basic_qc.flat_top_run_samples` | `int` | `5` | Runs of >= this many identical samples at the record extreme count as clipped. |
| `config.basic_qc.sta_lta_enabled` | `bool` | `True` | Anti-trigger transient screening with STA/LTA on \|x\|. |
| `config.basic_qc.sta_s` | `float` | `1.0` | Short-term average length (s). |
| `config.basic_qc.lta_s` | `float` | `30.0` | Long-term average length (s). |
| `config.basic_qc.sta_lta_min` | `float` | `0.2` | Window rejected if STA/LTA falls below this anywhere inside it. |
| `config.basic_qc.sta_lta_max` | `float` | `2.5` | Window rejected if STA/LTA exceeds this anywhere inside it. |
| `config.standard_selection` | `StandardSelection` | `(see nested fields)` | Standard reference selection options. |
| `config.standard_selection.frequency_outlier_rejection` | `one of 'none', 'fdwra_cox2020'` | `'none'` | Optional frequency-domain window rejection after basic QC (can alter the peak distribution). |
| `config.standard_selection.fdwra_n_std` | `float` | `2.0` | FDWRA rejection half-width in lognormal standard deviations. |
| `config.standard_selection.fdwra_max_iterations` | `int` | `50` | FDWRA iteration cap. |
| `config.peak_discovery` | `PeakDiscovery` | `(see nested fields)` | Shape-neutral peak-family discovery. |
| `config.peak_discovery.window_min_prominence_ratio` | `float` | `1.25` | Per-window local peak minimum topographic prominence (amplitude ratio). |
| `config.peak_discovery.kde_bandwidth_decades` | `float` | `0.03` | Gaussian KDE bandwidth (log10 decades) for clustering window peaks into families. |
| `config.peak_discovery.family_max_halfwidth_decades` | `float` | `0.12` | Maximum family half-width around its KDE mode (log10 decades). |
| `config.peak_discovery.kde_merge_valley_ratio` | `float` | `0.8` | Adjacent KDE modes merge when the density minimum between them is >= this fraction of the lower mode. |
| `config.peak_discovery.min_recurrence` | `float` | `0.3` | Minimum fraction of discovery windows with a peak inside the family. |
| `config.peak_discovery.family_listing_min_recurrence` | `float` | `0.1` | Families below this recurrence are not listed at all (clutter control only). |
| `config.peak_discovery.mean_min_prominence_ratio` | `float` | `1.5` | Discovery mean-curve local peak minimum prominence (amplitude ratio) for plausibility. |
| `config.peak_discovery.min_discovery_windows` | `int` | `5` | Minimum non-overlapping QC windows for discovery. |
| `config.peak_discovery.lower_edge_min_hv` | `float` | `1.5` | Rising curve at the lowest usable frequency with H/V >= this flags lower_band_unresolved. |
| `config.peak_discovery.narrow_line_min_contrast_ratio` | `float` | `3.0` | Raw-spectrum line contrast vs. its neighbourhood that counts as a narrow line. |
| `config.peak_discovery.narrow_line_max_width_bins` | `int` | `3` | Maximum width (FFT bins) of a narrow line. |
| `config.peak_discovery.narrow_line_min_persistence` | `float` | `0.8` | Fraction of windows in which the line must recur to be persistent. |
| `config.peak_discovery.smoothing_sensitivity_bandwidth` | `float` | `80.0` | Alternate (narrower) Konno-Ohmachi b used for the smoothing-sensitivity diagnostic. |
| `config.sdof` | `SDOFMatching` | `(see nested fields)` | Peak-aligned template matching and selection. |
| `config.sdof.template_bank` | `str` | `'auto'` | SDOF bank ID; 'auto' selects the shipped bank conditioned for this run's Konno-Ohmachi b if available. |
| `config.sdof.extra_banks` | `tuple` | `()` | Optional additional template families (e.g. 2DOF/3DOF bank IDs), reported separately. |
| `config.sdof.peak_interpolation` | `one of 'parabolic_log', 'none'` | `'parabolic_log'` | Peak estimator: 3-point parabola in (ln f, ln A), offset bounded to +/- half a grid step. |
| `config.sdof.min_fft_bins_in_fit_band` | `int` | `10` | Minimum original FFT bins inside [fp/2, 2 fp] for a rankable fit. |
| `config.sdof.min_fft_bins_below_peak` | `int` | `4` | Minimum original FFT bins inside [fp/2, fp). |
| `config.sdof.min_log_variance` | `float` | `1e-06` | Weighted variance of y below this makes R-squared undefined (unrankable). |
| `config.sdof.r2_min` | `float` | `0.0` | Eligibility requires R2_log strictly greater than this (0 = better than a constant). |
| `config.sdof.calibration_profile` | `str \| None` | `None` | Optional offline calibration profile ID supplying stronger eligibility thresholds. |
| `config.sdof.selection_mode` | `one of 'automatic', 'manual_top_n'` | `'automatic'` | automatic: all greedy survivors; manual_top_n: first n (selection-conditional). |
| `config.sdof.manual_n` | `int \| None` | `None` | Requested count for manual_top_n. |
| `config.uncertainty` | `Uncertainty` | `(see nested fields)` | Dependence-aware bootstrap settings. |
| `config.uncertainty.n_bootstrap` | `int` | `2000` | Bootstrap replicates for final results. |
| `config.uncertainty.seed` | `int` | `20261020` | Deterministic bootstrap seed (recorded). |
| `config.uncertainty.min_informative_blocks` | `int` | `10` | Experimental guard: fewer informative blocks returns ci_not_estimable. |
| `config.uncertainty.regular_spacing_max_ratio` | `float` | `1.5` | Retained sequence is 'regular' if max/min spacing between consecutive windows <= this. |
| `config.uncertainty.irregular_block_lw_factor` | `float` | `5.0` | Irregular sequences: original-time block length = factor * Lw (before dependence doubling). |
| `config.uncertainty.sensitivity_factors` | `tuple` | `(0.5, 2.0)` | Predeclared block-length multipliers reported as sensitivity (never chosen by width). |
| `config.uncertainty.adjacent_block_corr_max` | `float` | `0.3` | If lag-1 correlation of block-mean features exceeds this, block length is doubled. |
| `config.uncertainty.max_block_doublings` | `int` | `3` | Maximum dependence-driven block doublings. |
| `config.uncertainty.sensitivity_width_ratio_limit` | `float` | `1.5` | Band width ratio across sensitivity lengths above this marks ci_limited. |
| `report` | `ReportOptions` | `(see nested fields)` | Reporting options. |
| `report.evaluate_sesame` | `bool` | `True` | Evaluate SESAME criteria (reporting only; never changes scientific results). |
| `display` | `DisplayOptions` | `(see nested fields)` | Display options. |
| `display.show_individual_windows` | `bool` | `False` | Overlay selected individual window curves. |
| `display.show_alternate_method` | `bool` | `False` | Overlay the other method's mean curve. |
| `display.figure_width_in` | `float` | `9.0` | Static figure width (inches). |
| `display.figure_height_in` | `float` | `6.0` | Static figure height (inches). |
| `display.dpi` | `int` | `150` | PNG resolution. |
| `robustness_check` | `bool` | `True` | Plan run B (1.5 x Lw, other settings unchanged) before results are examined. |
| `robustness_lw_factor` | `one of 1.5` | `1.5` | Run B window-length multiplier (fixed in v1). |
