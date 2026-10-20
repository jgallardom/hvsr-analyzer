# Scientific and implementation decision log

Versioned record of every convention that the specification leaves open, and every original design
decision of this project. Established methods are cited. Project choices are labeled **project
decision** and are NOT endorsed by SESAME, hvsrpy, Geopsy, or any other source.

Format: `D-NNN (date, algorithm version)`. Superseded decisions remain listed.

## Licensing and dependencies

**D-001 (2026-10-20).** Software license BSD-3-Clause (chosen by the repository owner). Submitted data
and derived results are CC BY 4.0 (spec 3.1). hvsrpy is GPL-3.0-or-later, so it is used only as an
external numerical oracle in tests (`reference` dependency group). Shipped code never imports it and
the Docker image does not install it. ObsPy is LGPL-3.0 (dynamic use as a library is compatible).
`arch` (NCSA license) provides the Politis-White block-length estimator.

## Preprocessing and spectra (prep-v1, fas-ko-sparse-v1)

**D-010.** Operation order: parse -> validate -> align channels -> select analyzed time span (integer
sample bounds) -> optional scalar sensitivity correction -> optional record-level Butterworth filter
(off by default; recorded as a derived operation) -> per window: remove mean, detrend (linear by
default), Tukey taper (alpha = 0.1) -> rFFT without zero padding -> amplitude `|X| / fs`.

**D-011.** Horizontal combination of raw Fourier amplitudes, then Konno-Ohmachi smoothing of H and V,
then H/V. This is the hvsrpy 2.x order (`traditional_hvsr_processing`). Smoothing the components
separately before combination would give slightly different values; this is a convention, recorded
in every analysis as `spectral.processing_order`.

**D-012.** Konno-Ohmachi window `W = [sin(b log10(f/fc)) / (b log10(f/fc))]^4`, `W = 1` at `f = fc`,
normalized to unit sum over nonzero FFT bins with `|b log10(f/fc)| <= 3` (hvsrpy's truncation; the
omitted tail weighs < 5e-6 of the peak). Implemented as a cached sparse (CSR) operator per
(sample rate, window samples, grid, b, support). A grid point is invalid when it has fewer than
`min_smoothing_bins` (default 3) nonzero FFT bins in support (hvsrpy would return 0 there; we
return invalid).

**D-013.** Analysis grid anchored at `10^(i/200)`, spanning `[fmin_search/4, 4 fmax_search]` by default
(so SESAME `[f0/4, 4 f0]` checks can be evaluated), restricted to `< 0.8 * Nyquist` (anti-alias
roll-off) and to the declared usable band when provided. Frequencies outside are recorded as invalid
with a reason, not dropped silently.

**D-014.** Vertical guard: a smoothed V amplitude that is non-finite, `<= 0`, or below
`1e-9 x` the window's median smoothed V makes that bin invalid for that window. No floor value is
substituted.

**D-015.** Mean curves (discovery reference, Standard, SDOF selections) are `exp(mean ln(H/V))` over
the windows valid at each bin, without weights. A mean bin is valid when the grid point is valid and
at least half of the windows (and at least one) are valid there; the per-bin window count is stored.

**D-016.** Numerical agreement with hvsrpy 2.1.0 (test oracle, `tests/stage_02/test_hvsrpy_reference.py`):
fed identical half-open windows, linear detrend, Tukey 0.1, `fft_settings={"n": None}` (hvsrpy
otherwise zero-pads to >= 2^15 samples, which changes the spectral sampling) and our grid frequencies,
per-window and lognormal-mean H/V agree to relative 1e-9 for RMS and geometric combinations. The
tolerance only covers float64 summation order. hvsrpy returns NaN (0/0) where no FFT bin falls in the
smoothing support; we mark those points `insufficient_smoothing_bins`.

## Window screening and the Standard reference (basic-qc-v1, std-earliest-nonoverlap-v1)

**D-020.** Basic QC (applied identically to every method): windows never cross gaps (structural);
clipping = any sample with `|x| >= 0.995 * full_scale` when full scale is declared, or a run of
`>= 5` identical samples at the record's extreme value; transient/stationarity = classic STA/LTA on
`|x - mean|` with STA 1 s, LTA 30 s, window rejected if the ratio leaves `[0.2, 2.5]` anywhere inside
it on any component. These thresholds are the values commonly used for H/V anti-triggering (e.g.
Geopsy's documented defaults; verify against your installed version). They are implementation
conventions and research parameters, not SESAME constants. STA/LTA is computed on the prepared
(optionally filtered) segment; clipping on the raw recorded samples (full scale is in recorded units).
A window whose samples never change on some component is rejected as `dead_channel`. Windows that
start within the first LTA seconds of a segment are screened only where the ratio is defined and
carry the note `sta_lta_partially_evaluated`.

Known limitation (observed in Stage 2): when a component is dominated by narrow-band or low-frequency
energy (e.g. a strong resonance near 0.1 Hz, or microseisms on broadband sensors), a 1 s STA on |x|
dips below 0.2 near envelope minima and STA/LTA rejects most windows. A Hilbert-envelope
characteristic function was tried and reduced but did not remove the effect, so the established |x|
form is kept. The analysis warns when STA/LTA rejects more than half of the candidates and recommends
an STA of several dominant periods. The data-yield report makes the rejection visible.

**D-021 (project decision).** Standard reference = basic QC, then earliest-first non-overlapping
packing of passing candidates on the candidate step grid. It has no preferred oscillator shape.
Optional frequency-domain window rejection (Cox et al., 2020 style; our own implementation) is a
separate step, off by default. When it is enabled, a basic-QC-only audit reference
(`standard_basic_audit`) is also reported.

## Peak discovery (peak-families-kde-v1, narrow-line-v1) - project decisions

**D-030.** Discovery uses the Standard reference windows (quality-screened, non-overlapping), so a
time interval can contribute at most once. Per window: local maxima of ln(H/V) inside the search band
with topographic prominence ratio >= 1.25 (edge maxima are not peaks). Peak log10-frequencies are
clustered with a Gaussian KDE (bandwidth 0.03 decades) evaluated on the analysis grid; adjacent KDE
modes are merged when the density minimum between them is >= 0.8 x the lower mode; each (merged)
mode defines a family bounded by the adjacent KDE minima (the minimum belongs to the lower family),
clipped to +/- 0.12 decades around the highest mode. Families are disjoint and numbered F1, F2, ...
by frequency. Recurrence = fraction of discovery windows with at least one peak inside the family;
families below 0.1 are not listed. A family is plausible when recurrence >= 0.3 AND the discovery
geometric-mean curve has a local maximum inside the family with prominence ratio >= 1.5. All are
research thresholds requiring null/contamination tests (Stage 5), not SESAME rules.

Observed in Stage 2: on flat synthetic records, per-window noise peaks give low-frequency families
with recurrence 0.6-1.0 (wide families collect many small noise peaks), so recurrence alone does not
discriminate; the mean-curve prominence rule is what rejects them. Stage 5 must measure the false
discovery rate of the combined rule.

Interpretation status priority: `not_computable` (fewer than `min_discovery_windows` or no valid
band) > `lower_band_unresolved` > `possible_source_peak` (all plausible families are flagged lines) >
`no_clear_peak` > `single_plausible_peak` / `multiple_plausible_peaks` (counting non-source plausible
families). All applicable conditions are also listed in `flags`.

**D-031.** Source-peak diagnostic: in the geometric-mean RAW (unsmoothed) component spectra, a line
inside a family whose contrast to its +/-20% neighbourhood median is >= 3 on at least two
components, width <= 3 bins, and recurring in >= 80% of windows flags `possible_source_peak`.
Harmonics (contrast >= 2 at 2x, 3x) and smoothing sensitivity (family peak amplitude with b = 80 vs
b = 40) are reported as evidence. Diagnostics never delete a peak.

**D-032.** `lower_band_unresolved`: the discovery mean curve is rising toward the lowest usable
frequency of the search band (its maximum over `[f_low, 2 f_low]` is at `f_low`) with H/V >= 1.5.
The fundamental candidate is then null because a lower mode may lie outside coverage.

**D-033.** Fundamental candidate = lowest plausible family not flagged as a possible source peak,
reported only when the lower band is resolved and the context is not `building_upper_floor` or
`structure_other` (those contexts never produce a free-field site-frequency candidate).

## Peak-aligned template matching (sdof-r2log-q129-v1, sdof-greedy-nonoverlap-v1)

**D-040.** Per-window peak estimate: grid maximum inside the family band that is a strict local
maximum of the smoothed per-window H/V, refined by a 3-point parabola in (ln f, ln A) with the offset
bounded to +/- half a grid step. fp and Ap are frozen before any template is evaluated.

**D-041.** Window values on the q grid come from linear interpolation of ln(H/V) in ln f between
analysis-grid points (monotone, no overshoot). The fit band [fp/2, 2 fp] must lie entirely inside
valid grid points; otherwise the candidate is not rankable.

**D-042.** Default SDOF bank conditioning: the shipped banks include raw templates and templates
smoothed with the continuous-limit Konno-Ohmachi operator for b in {20, 30, 40, 50, 60, 80, 100}.
`template_bank = "auto"` picks the bank matching the run's b. The conditioned peak is located and
normalized AFTER smoothing. Smoothing a theoretical ratio is not the same as smoothing component
spectra before division; the discrepancy is benchmarked in `tests/stage_03` and reported in
`docs/template-banks.md`.

**D-044 (bank composition).** Shipped banks and their measured behavior are listed in
`docs/template-banks.md`: SDOF raw + continuous-limit Konno-Ohmachi conditioned (b = 20 ... 100);
2DOF/3DOF uniform-ratio shear chains with equal modal damping, raw + b = 40. The conditioning
benchmark showed that raw templates against smoothed data bias the damping label upward (24 / 54
correct vs 54 / 54 for conditioned), which supports `auto` = conditioned.

**D-045 (fit-band bracket).** To interpolate y at exactly fp/2 and 2 fp, the grid point immediately
outside each end (at most 0.5 % in frequency) is used and must be valid. No other data outside
[fp/2, 2 fp] enters the score. The peak estimator itself uses the family band (it defines the
candidate peak and is not part of the comparison).

**D-043.** R-squared undefined when the weighted variance of y is below 1e-6. Template ties (exactly
equal R2) choose the first template in bank order (lowest damping) and set `tie` flag. A best template
at the first or last damping value sets `grid_boundary_best`.

## Uncertainty (temporal-block-bootstrap-v1) - project decisions

**D-050.** Regular retained sequences (max/min start spacing <= 1.5, one session) use a moving-block
bootstrap; block length = ceil(max over predeclared features of the Politis-White (2004, with Patton
et al. 2009 correction) circular-block estimate from `arch`). Irregular sequences use original-time
blocks of `5 x Lw` seconds (cluster bootstrap over non-empty blocks, ratio estimator accounting for
variable counts). If lag-1 correlation of block-mean features exceeds 0.3, block length doubles (max
3 times). Fewer than 10 informative blocks -> `ci_not_estimable`. Sensitivity at 0.5x and 2x block
length is reported; a width ratio > 1.5 marks `ci_limited`. The band is nominal and uncalibrated.

*Revision (Stage 4, before any validation or coverage run).* Irregular-sequence base block length
changed from `5 x Lw` to `2 x Lw`. Reason: with Lw = 50 s, a 20-30 min recording (the SESAME-typical
duration) contains only 5-7 blocks of 250 s, so the 10-block guard would make the CI unestimable for
essentially every SDOF selection. This is an estimability argument, not a band-width argument; the
dependence safeguard is the data-driven doubling rule, which only ever lengthens blocks. Further
details fixed at implementation:
- Block membership, adjacency tests and features use ONLY accepted windows; rejected periods and
  rejection counts never enter, so adding rejected data outside accepted windows cannot change the band.
- Irregular dependence test: lag-1 correlation between means of blocks that are adjacent in original
  time (block indices k and k+1, both non-empty). Fewer than 4 such pairs -> dependence not assessable
  -> `ci_limited` (the band is still reported, with the reason).
- The regular path uses the circular moving-block bootstrap (Politis & Romano 1992), matching the
  circular Politis-White estimate. Blocks never contain partial windows.
- Replicate mean = sum c_w ln HV_w / sum c_w over windows valid at each bin (ratio estimator), with
  replicate counts c drawn once per replicate for all frequencies jointly.
- Any bin with zero bootstrap width (<= 1e-12 in ln) is reported as null and the status becomes
  `ci_limited`; zero width is never exported.
- Sensitivity replicates reuse the seed. The reported band is always the base-length band, never the
  narrowest one. A sensitivity length with fewer than 10 blocks is reported as `not_estimable` with a
  note; the status becomes `ci_limited` only if no alternative length could be evaluated or the
  evaluated widths differ by more than 1.5x.

*Revision 2 (Stage 4, still before any validation run): small-sample behavior of the dependence rules.*
A null simulation (iid Gaussian features, 300-4000 replicates; script reproduced in
`tests/stage_04/test_block_rules.py`) showed the following:
- Politis-White, taking the maximum over 4 features, has a median of 4.1 windows and a 90th percentile
  at the cap of 8 when n = 24. That makes the CI unestimable in 83 % of iid cases at n = 24, 24 % at
  n = 50, 7 % at n = 75 and 4 % at n = 100. This is noise from the estimator, not detected dependence.
- The raw rule "lag-1 r > 0.3" fires falsely in 31 % of iid cases with 11 block pairs and in 40 % with 5.

Changes:
1. Politis-White is used only for regular sequences with at least `pw_min_windows = 75` windows. Shorter
   regular sequences use the original-time block rule, i.e. 2 x Lw blocks.
2. Blocks are doubled only when the maximum lag-1 correlation over the p features exceeds both the
   materiality threshold 0.3 and the one-sided Bonferroni bound z(1 - 0.05/p)/sqrt(pairs). The measured
   false-doubling rate is <= 2.3 % for 5-40 pairs.
3. When the critical r exceeds 0.5, a low-power note states which correlations are detectable.
   Undetected weak dependence remains a known limitation, to be evaluated in the Stage 5 coverage study.

**D-051.** Predeclared dependence features: ln(H/V) at the selection's mean-curve peak (or, without a
family, at the global maximum of the search band), the mean ln(H/V) over [fp/2, fp/sqrt2] and
[sqrt2 fp, 2 fp], and ln(window fp) when defined. Features with any undefined value in the selection
are dropped from the dependence assessment (recorded); with no usable feature the CI is
`ci_not_estimable`.

Peak replicates (peak-replicate-v1): each replicate mean curve is searched for a strict local maximum
inside the family band, with no realignment. The fractions with a peak in the family, with no peak, and
with the search-band global maximum in another listed family are all reported. The 2.5-97.5 %
interval of replicate peak frequencies is explicitly conditional on a peak existing in the family.
With at least 20 such replicates, a Gaussian KDE in ln f (Silverman bandwidth, at least one grid step)
is computed; two or more modes each holding >= 10 % of the mass mark the peak `ambiguous_multimodal`
and all major modes are reported.
