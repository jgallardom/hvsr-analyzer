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

## Window screening and the Standard reference (basic-qc-v1, std-earliest-nonoverlap-v1)

**D-020.** Basic QC (applied identically to every method): windows never cross gaps (structural);
clipping = any sample with `|x| >= 0.995 * full_scale` when full scale is declared, or a run of
`>= 5` identical samples at the record's extreme value; transient/stationarity = classic STA/LTA on
`|x - mean|` with STA 1 s, LTA 30 s, window rejected if the ratio leaves `[0.2, 2.5]` anywhere inside
it on any component. These thresholds are the values commonly used for H/V anti-triggering (e.g.
Geopsy's documented defaults; verify against your installed version). They are implementation
conventions and research parameters, not SESAME constants.

**D-021 (project decision).** Standard reference = basic QC, then earliest-first non-overlapping
packing of passing candidates on the candidate step grid. It has no preferred oscillator shape.
Optional frequency-domain window rejection (Cox et al., 2020 style; our own implementation) is a
separate step, off by default. When it is enabled, a basic-QC-only audit reference
(`standard_basic_audit`) is also reported.

## Peak discovery (peak-families-kde-v1, narrow-line-v1) - project decisions

**D-030.** Discovery uses the Standard reference windows (quality-screened, non-overlapping), so a
time interval can contribute at most once. Per window: local maxima of ln(H/V) inside the search band
with topographic prominence ratio >= 1.25 (edge maxima are not peaks). Peak log10-frequencies are
clustered with a Gaussian KDE (bandwidth 0.03 decades); each KDE mode defines a family bounded by
the adjacent KDE minima, clipped to +/- 0.12 decades. Recurrence = fraction of discovery windows with
at least one peak inside the family. A family is plausible when recurrence >= 0.3 AND the discovery
geometric-mean curve has a local maximum inside the family with prominence ratio >= 1.5. All are
research thresholds requiring null/contamination tests (Stage 5), not SESAME rules.

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

**D-051.** Predeclared dependence features: ln(H/V) at the selection's mean-curve peak (or, without a
family, at the global maximum of the search band), the mean ln(H/V) over [fp/2, fp/sqrt2] and
[sqrt2 fp, 2 fp], and ln(window fp) when defined.
