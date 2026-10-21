# Data dictionary

Generated from `hvsr_core.exports.data_dictionary()` by `scripts/gen_docs.py`. Do not edit by hand.
The same content is served as JSON at `GET /api/v1/data-dictionary` (schema `data_dictionary`).

Schema version 1.0.0; export format `tables-v1`.

## CSV conventions

These apply to every numeric table below.

| Convention | Rule |
| --- | --- |
| encoding | UTF-8 without byte-order mark |
| delimiter | , |
| decimal_separator | . |
| header_rows | 1 |
| comment_lines | none |
| line_terminator | \n |
| booleans | true / false |
| floats | shortest round-trip decimal representation (parses to the identical binary64 value) |
| unavailable | empty cell; the row's `reason` column states why. Zero is never a placeholder. |
| times | UTC ISO 8601 with a trailing Z; offsets in seconds from the first canonical sample |

Waveform files are not numeric tables. A retained original is served byte for byte, whatever its
own layout. A Reconstructed CSV (canonical-only storage) begins with one `#` line stating that the
original bytes were not retained, then the original's preserved comment lines, header, and
delimiter; formatting outside the preservation contract is not reproduced. Use the
`normalized_waveform` table for a fixed layout.

## `mean_spectrum`: Mean H/V spectrum

Each row is one selection (method / peak family) at one frequency of the analysis grid.

Selected-window geometric-mean H/V with the single nominal 95% pointwise band ('Estimated 95% pointwise CI for the selected-window mean'), exactly as in the analysis JSON `selections[].mean_curve`. The band is selection-conditional and its coverage is not calibrated unless the analysis JSON `calibration.status` says otherwise.

| Column | Type | Unit | Nullable | Description |
| --- | --- | --- | --- | --- |
| `selection_id` | string |  | no | Selection identifier, as in the analysis JSON. |
| `peak_family_id` | string |  | yes | Peak family of the selection; empty when there is none. |
| `frequency_hz` | number | Hz | no | Frequency of the anchored logarithmic analysis grid. |
| `hv_geometric_mean` | number | dimensionless (H/V) | yes | Geometric mean of the selected windows' H/V (ln-amplitude average). |
| `ci95_lower` | number | dimensionless (H/V) | yes | Lower bound of the Estimated 95% pointwise CI for the selected-window mean. |
| `ci95_upper` | number | dimensionless (H/V) | yes | Upper bound of the Estimated 95% pointwise CI for the selected-window mean. |
| `count` | integer | windows | no | Selected windows valid at this frequency. |
| `log_std` | number | natural-log units of H/V | yes | Sample standard deviation of ln H/V across the selected windows (dispersion, not the CI). |
| `frequency_valid` | boolean |  | no | The analysis grid is valid at this frequency. |
| `status` | string |  | no | Row status. One of: `ok`, `ci_limited`, `ci_unavailable`, `mean_unavailable`, `frequency_invalid`. |
| `reason` | string |  | yes | Why each empty numeric cell in this row is unavailable ('column: reason', separated by ' \| '). Empty when every value is present. |

## `window_table_standard`: Per-window table, Standard method

Each row is one candidate window of one Standard selection (every candidate, selected or not).

Candidate windows with basic-QC result and the Standard rule outcome (earliest-first non-overlapping windows; optional FDWRA).

| Column | Type | Unit | Nullable | Description |
| --- | --- | --- | --- | --- |
| `selection_id` | string |  | no | Selection identifier, as in the analysis JSON. |
| `window_id` | string |  | no | Stable candidate window ID derived from the start sample. |
| `start_sample` | integer | samples | no | Window start, samples from the first canonical sample. |
| `start_s` | number | s | no | Window start, seconds from the first canonical sample. |
| `end_s` | number | s | no | Window end (exclusive), seconds from the first canonical sample. |
| `start_utc` | string | UTC ISO 8601 | yes | Absolute window start; empty when the recording has no absolute start time. |
| `segment_index` | integer |  | no | Contiguous recording segment containing the window. |
| `qc_passed` | boolean |  | no | Basic window quality control passed. |
| `qc_reasons` | string |  | yes | Basic-QC rejection reasons separated by ';' (empty when passed). |
| `outcome` | string |  | no | Outcome of the Standard rule. One of: `selected`, `qc_rejected`, `overlap_excluded`, `fdwra_rejected`. |
| `selection_order` | integer |  | yes | 1-based position in the selection (time order). |
| `peak_family_id` | string |  | yes | Peak family used for the per-window peak; empty when there is none. |
| `window_peak_frequency_hz` | number | Hz | yes | Strict in-family peak of this window's H/V (parabolic refinement). |
| `reason` | string |  | yes | Why each empty numeric cell in this row is unavailable ('column: reason', separated by ' \| '). Empty when every value is present. |

## `window_table_sdof`: Per-window table, SDOF-selected method

Each row is one candidate window of one peak family (every candidate, selected or not).

Peak-aligned SDOF template matching (R2_log on the 129-point ln-q grid), eligibility, ranking, and greedy non-overlapping selection inside each plausible peak family.

| Column | Type | Unit | Nullable | Description |
| --- | --- | --- | --- | --- |
| `selection_id` | string |  | no | Selection identifier, as in the analysis JSON. |
| `peak_family_id` | string |  | no | Peak family in which the window was ranked. |
| `window_id` | string |  | no | Stable candidate window ID derived from the start sample. |
| `start_sample` | integer | samples | no | Window start, samples from the first canonical sample. |
| `start_s` | number | s | no | Window start, seconds from the first canonical sample. |
| `end_s` | number | s | no | Window end (exclusive), seconds from the first canonical sample. |
| `start_utc` | string | UTC ISO 8601 | yes | Absolute window start; empty when the recording has no absolute start time. |
| `segment_index` | integer |  | no | Contiguous recording segment containing the window. |
| `qc_passed` | boolean |  | no | Basic window quality control passed. |
| `qc_reasons` | string |  | yes | Basic-QC rejection reasons separated by ';' (empty when passed). |
| `eligibility` | string |  | no | Matching eligibility status. One of: `qc_rejected`, `eligible`, `no_meaningful_match`, `no_peak_in_family_band`, `incomplete_fit_band`, `outside_common_band`, `insufficient_resolution`, `undefined_r2`. |
| `outcome` | string |  | no | Selection outcome. One of: `selected`, `qc_rejected`, `not_eligible`, `overlap_excluded`, `beyond_requested_n`. |
| `window_peak_frequency_hz` | number | Hz | yes | Bank-independent in-family peak frequency fp of this window. |
| `window_peak_amplitude` | number | dimensionless (H/V) | yes | H/V amplitude Ap at fp. |
| `fit_band_lower_hz` | number | Hz | yes | Template fit band lower edge fp/2. |
| `fit_band_upper_hz` | number | Hz | yes | Template fit band upper edge 2 fp. |
| `r2_log` | number | dimensionless | yes | Best-template R2_log (template resemblance, not a quality probability). |
| `r2_rank` | integer |  | yes | 1-based rank among eligible windows: descending R2_log, then earlier start, then window ID. |
| `selection_order` | integer |  | yes | 1-based position in the greedy selection order. |
| `template_index` | integer |  | yes | Best template index in the bank. |
| `template_parameters` | string |  | yes | Best template parameters 'name=value' separated by '; '. |
| `flags` | string |  | yes | Matching flags separated by ';'. |
| `reason` | string |  | yes | Why each empty numeric cell in this row is unavailable ('column: reason', separated by ' \| '). Empty when every value is present. |

## `window_spectra`: Per-window H/V spectra

Each row is one candidate window at one valid frequency of the analysis grid.

Smoothed H/V of every candidate window (selected or not) at the frequencies where the analysis grid is valid (the full grid and its validity are in the analysis JSON). Regenerated deterministically from the stored recording and the run configuration.

| Column | Type | Unit | Nullable | Description |
| --- | --- | --- | --- | --- |
| `window_id` | string |  | no | Stable candidate window ID. |
| `start_sample` | integer | samples | no | Window start, samples from the first canonical sample. |
| `frequency_hz` | number | Hz | no | Analysis grid frequency. |
| `hv` | number | dimensionless (H/V) | yes | Window H/V amplitude. |
| `reason` | string |  | yes | Why each empty numeric cell in this row is unavailable ('column: reason', separated by ' \| '). Empty when every value is present. |

## `normalized_waveform`: Normalized waveform

Each row is one sample instant (gaps appear as jumps in sample_index; nothing is interpolated).

Three-component samples exactly as parsed (integers for integer data, shortest round-trip decimals for floating data). Readable without decoding MiniSEED.

| Column | Type | Unit | Nullable | Description |
| --- | --- | --- | --- | --- |
| `sample_index` | integer | samples | no | Index from the first canonical sample. |
| `time_offset_s` | number | s | no | sample_index divided by the sampling rate. |
| `time_utc` | string | UTC ISO 8601 | no | Absolute sample time (nanosecond digits). Present only when the recording has an absolute start time. |

Optional columns: `time_utc`.

Columns matching `^N_[A-Za-z0-9_]+$` (number): N component sample; the header suffix is the declared units ('/' as '_per_', 'unknown_units' when undeclared).

Columns matching `^E_[A-Za-z0-9_]+$` (number): E component sample; the header suffix is the declared units ('/' as '_per_', 'unknown_units' when undeclared).

Columns matching `^Z_[A-Za-z0-9_]+$` (number): Z component sample; the header suffix is the declared units ('/' as '_per_', 'unknown_units' when undeclared).

## JSON documents

| Document | Description |
| --- | --- |
| `analysis` | Per-run, per-method analysis (schema: analysis) |
| `manifest` | Per-measurement index of every artifact (schema: manifest) |
| `window_table` | JSON form of the per-window tables (schema: window_table) |
| `plot_recipe` | Figure recipe stored with every plot artifact (schema: plot_recipe) |
| `data_dictionary` | This document (schema: data_dictionary) |

## Units used in the analysis JSON

| Quantity | Unit |
| --- | --- |
| `frequency` | Hz |
| `hv` | dimensionless amplitude ratio (H/V) |
| `time` | s |
| `log_std` | natural-log units of H/V |
| `ci` | same units as hv; bounds of the selected-window geometric mean |
| `window_start_samples` | samples from the first canonical sample |

## Kinds of content

| Kind | Manifest categories |
| --- | --- |
| measured_data | original_data, canonical_recordings, sensor_metadata |
| algorithmic_results | analysis_runs, spectra, plots |
| experimental_diagnostics | quality_evaluation (robustness comparison), calibration status |
| human_interpretation | annotations (none are generated automatically) |
