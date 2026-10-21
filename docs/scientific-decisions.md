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

**D-052 (temporal-block-bootstrap-v2; Stage 5 development set, before the protocol lock).** The band
uses expanded percentile levels (Hesterberg 2015, *The American Statistician* 69:371-386): with k
informative blocks, q = Phi(-sqrt(k/(k-1)) * t_{0.975, k-1}), and the band is the [q, 1-q] replicate
quantiles (k = 18 gives q = 1.5 %). Reason: a plain percentile bootstrap over k resampling units
ignores both the (k-1)/k variance shrinkage and the t-distribution of the studentized mean. Its
expected coverage is about 92.6 % for k = 18 iid units. On the development set (never the test
set), the null scenario showed 43/49 = 88 % with plain percentiles. The correction is the textbook
form, has no tunable parameter, and only widens the band; it vanishes as k grows.

Peak replicates (peak-replicate-v1): each replicate mean curve is searched for a strict local maximum
inside the family band, with no realignment. The fractions with a peak in the family, with no peak, and
with the search-band global maximum in another listed family are all reported. The 2.5-97.5 %
interval of replicate peak frequencies is explicitly conditional on a peak existing in the family.
With at least 20 such replicates, a Gaussian KDE in ln f (Silverman bandwidth, at least one grid step)
is computed; two or more modes each holding >= 10 % of the mass mark the peak `ambiguous_multimodal`
and all major modes are reported.

**D-053 (Stage 5 outcome).** The locked validation (protocol hvsr-mc-validation-v1) did not meet its
decision rule (docs/validation/report-v1.md). Therefore:
- every CI is labelled "Estimated 95% pointwise CI for the selected-window mean" with
  `coverage_calibrated = false` and a pointer to the validation report;
- analysis outputs state that coverage failed under minute-scale regime changes and that default
  anti-trigger QC fails for strongly low-frequency-dominated records;
- the peak-frequency interval is described as conditional and never as a 95 % interval for the
  site resonance;
- fixing either failure requires a method change and a NEW protocol version with fresh locked seeds;
  v1 seeds must not be reused for tuning.

## Stage 6

**D-060 (robust-lw1.5-v1; fixed before any comparison result was examined).** Run B uses
round(1.5 x Lw x fs) samples, with every other scientific setting unchanged (`robustness_config`).
If B cannot be computed, the comparison is `not_evaluable` with the reason, and no alternative B is
searched for. Both methods are attempted in both runs.

Matching and resolution:
- Peak families are matched by the closest fp within |log10 ratio| <= 0.1.
- A family is adequately resolved when fp x Lw_A >= 10 cycles and max(df_A, df_B)/fp <= 0.1.

Agreement tolerances, applied only to matched, adequately resolved families:
- |ln(fp_B/fp_A)| <= 0.05;
- |ln(Ap_B/Ap_A)| <= 0.20;
- the median |ln HV_B - ln HV_A| over valid search-band bins of the common frequency grid <= 0.10.

Reported but not judged: half-amplitude peak width (ln f), selected window counts, the fraction of
run A's selected time also selected by B, uncertainty statuses, FFT resolutions, and the max
|ln HV| difference.

Per-method status:
- `sensitive` if the plausible family sets differ or any tolerance fails;
- `consistent` if at least one resolved matched family exists and all checks pass;
- otherwise `not_evaluable`.

The overall status is `sensitive` if any method is sensitive, otherwise `consistent` if any method is.
Because the runs share a waveform, they are correlated: never averaged, never counted as
independent, and neither is preferred. The tolerances are research criteria checked on known
synthetic signals (AC-S6-06/07), not SESAME rules.

**D-061 (quality dimensions).** The analysis reports three separate dimensions:
- usability: channels, clipping, duration, gaps, coverage, response status, non-overlapping windows
  and temporal groups;
- peak identifiability: prominence, recurrence, fp/Ap variation, competing plausible families and
  source-artifact flags;
- sensitivity: Standard vs SDOF, selection counts, and the Lw robustness comparison.

No 0-100 summary score is computed (`summary_score: null`, with a note). An optional score would need
an explicit formula, weights, inputs, version and limitations; none has been validated. A flat curve
and a Standard/SDOF disagreement are not penalized as poor recording quality.

## Stage 8 - presentation and exports (no scientific change)

None of these decisions changes a computed number. The Stage 5 fingerprinted files are untouched.

**D-080 (figure readability criterion, project decision).** "Readable" (spec 13.1) is defined as:
every text element of a served figure renders at >= 9 CSS px.
- Scope: viewports from 340 to 2560 CSS px, with and without a 17 px classic scrollbar.
- Layouts: pages serve a narrow-screen SVG (3.6 x 6.4 in, 9 pt text, 7.5 pt footer and legend)
  at <= 900 px, and the desktop SVG above that.
- Measurement: the rendered width comes from the stylesheet rules
  (`tests/stage_08/test_figures.py::rendered_width`), and font sizes are read from the served SVGs.
- Worst cases: 9.35 CSS px for the narrow figure at a 340 px viewport with a scrollbar; 9.84 CSS px
  for the desktop figure at 901 px with a scrollbar.
- Log-axis ticks: labeled as plain numbers (`0.1`, `1`, `10`) with no math-text superscripts.
  Superscript exponents rendered at 6.65 pt and 5.95 pt, below the criterion.
- Math text is off for all figure text, so record-derived strings (record codes, attributions) are
  drawn literally.
- Verification: a geometry check covers 16 layout configurations and confirms that no text
  overlaps or is clipped. Browser screenshots at 340 px and desktop width are in
  `docs/evidence/stage8/`.

**D-081 (localization boundary).** Every interface string comes from the English catalog
(`hvsr_core/locales/en.json`) through `t()`. Text that is data and must not be translated is
rendered inside `translate="no"` elements:
- identifiers, statuses, and enum values;
- user metadata, attributions, and stored artifact labels;
- diagnostic sentences produced by the scientific core;
- data-dictionary text;
- the consent wording. It is versioned and hashed (`consent-v1`), so it is legal text shown exactly
  as agreed, marked `lang="en"`.

API `detail` fields are English diagnostics next to machine-readable `error` codes. The boundary is
machine-checked: a pseudo-locale wraps every catalog value, and a test fails on any letters outside
the catalog or a `translate="no"` element.

**D-082 (cosmetic re-renders).** A figure variant is a new plot artifact derived from the stored
analysis JSON. Its identity is the hash of its recipe, so identical requests share one artifact. It
lists the analysis JSON (and per-window spectra when windows are drawn) in `source_refs`. It never
replaces the published figure.
- Bounded set: variants at the default size and resolution are finite (selection x overlays x
  format x layout). They are created on demand by the results page without a token.
- Custom size or dpi: requires an upload token and is capped per run
  (`HVSR_MAX_PLOT_VARIANTS_PER_RUN`).
- PNG size: every PNG, published or re-rendered, is bounded by `HVSR_MAX_PLOT_PIXELS`.
- Verification: the tests replace the FFT, the core run, and the bootstrap with failing stubs while
  re-rendering. A control shows that the same stubs do stop a real analysis.

**D-083 (exports).** Mean-spectrum and per-window CSVs are exact projections of the analysis
JSON:
- Floats are written in shortest round-trip form, so CSV and JSON values agree bit for bit.
- The figure recipe records `series_sha256` over the same arrays.
- Per-window spectra are not stored for every run. They are regenerated on request from the stored
  recording and run configuration, bounded by `HVSR_MAX_WINDOW_SPECTRA_CELLS`, and the science key
  is re-checked first.

A Reconstructed CSV (canonical-only storage, Stage 1 design) keeps its leading `#` disclosure line.
It is a waveform file, not a numeric table, so the tables' "no comment lines" rule does not apply to
it. The data dictionary states this.

**D-084 (licensing inside analysis documents).** Each published analysis JSON embeds the fixed part
of its measurement's licensing: license ID and URL, attribution, source citation, third-party note,
record code, and manifest URL. The manifest remains the current record, with modification history
and moderation state. Those can change, so they are not copied into immutable documents.

**D-085 (external reference URLs).** Analysis documents and `docs/sesame-implementation.md` link
the SESAME guidelines as `http://sesame.geopsy.org/Papers/HV_User_Guidelines.pdf`. The
specification lists the `https://` form; the address is otherwise the same. The CC BY 4.0 link is
`https://creativecommons.org/licenses/by/4.0/`. These are the only external links in published
documents (the manifest-link test keeps an explicit allowlist). The tests do not fetch them, and
their availability has not been checked.

**D-086 (example exports).** `docs/examples/` holds unedited downloads of one real analysis: a
synthetic SDOF response (fn = 2 Hz, damping 0.2, 1800 s at 50 Hz, seed 21). A synthetic recording
is used because it can be regenerated and redistributed. The files are tested to:
- match their manifest checksums;
- validate against the published schemas;
- equal a fresh analysis of the same recording (CSV bytes and the JSON `grid` and `selections`).

The per-window spectra (about 1.8 MB compressed) and the normalized waveform are left out for size.

## Stage 9 - efficiency, security, and deployment (no scientific change)

None of these decisions changes a computed number. The Stage 5 fingerprinted files are unchanged,
and the replay and regression tests pass.

**D-090 (job isolation).** Each job runs in a fresh spawned child process (`isolation.py`):
- The CPU and address-space limits are set after imports, relative to the child's own baseline, so
  the limit measures the job, not the interpreter.
- Exhausting CPU, memory, or wall time is a permanent failure that names the limit: the same input
  would exhaust it again, so retrying only wastes the budget.
- Any other abnormal exit is retried up to `HVSR_JOB_MAX_ATTEMPTS`, because it may be transient.
- Spawn, not fork: the child inherits no database connections, locks, or thread pools from the web
  process.

**D-091 (upload parsing off the event loop).** Upload handlers were `async`, but they parsed files
synchronously, so a large upload stalled every other request, including health checks. Uploads
(previews, submissions, banks) are now read and parsed in the thread pool, at most
`HVSR_MAX_CONCURRENT_UPLOADS` at a time per process. A further upload waits up to
`HVSR_UPLOAD_WAIT_S` and then gets 503 `busy` with `Retry-After`. It is not queued without bound,
because each parse holds memory proportional to the upload (`docs/performance.md`).

**D-092 (PostgreSQL serialization).** Three count-then-write steps race under PostgreSQL's READ
COMMITTED isolation, which SQLite's single writer had hidden:
- startup migrations;
- the job concurrency ceiling;
- concurrent retries of one submission key.

Each now takes a transaction-scoped advisory lock: one global lock for migrations, another for job
claims, and one per contributor for quota reservations. Throughput is unaffected at the planned scale (one
worker).

**D-093 (idempotency keys).** A submission key belongs to its contributor and binds to the first
accepted submission. A later request with the same key returns that measurement ("idempotent
replay: already accepted") and is not charged, whatever files it carries. File contents are not
compared, so clients must use a new key for each distinct submission. When concurrent retries race,
exactly one request is accepted and charges the quota. Its response reports the charged bytes;
every other request with that key gets `existing: true` and `charged_logical_bytes: 0`.

**D-094 (memory-bounded parsing).** CSV lines are streamed (`iter_lines`) with exactly the line
boundaries of `str.splitlines` (property-tested), and samples are stored in packed arrays. Readers
and errors are unchanged. StationXML is checked for a DOCTYPE by a parser with entities, DTD
loading, and network access disabled, so the check covers every encoding. Any DOCTYPE is rejected.

**D-095 (methods without a selection).** When a method selects no windows (for example, no SDOF
match), there is no curve to draw:
- the run is still published, with the analysis JSON stating the method status and reason;
- no figure is produced for that method, rather than an empty or substitute plot;
- the results page shows the status and reason in place of the figure and peak sections.

**D-096 (uploaded template banks).** Banks are uploaded as data only, through the same validator as
shipped banks. Uploaded bank versions are immutable and never deleted, so every run stays
replayable. Only shipped banks and active uploaded banks can be selected for new analyses. The
object store and database are the durable copy. The local bank directory is a verified cache,
rebuilt as needed, because hosted container disks are ephemeral.

**D-097 (deployment topology and hosted limits).** The deployment is one Render web service with
the worker embedded, as the specification prefers. A separate worker is not added: one job at a
time on one CPU handles the projected volumes (`docs/costs.md`, capacity check). The hosted limits
are derived from measured memory, not guessed:
- a web process peaks at about baseline + gzip bytes + 2 × decoded text + 32 bytes per sample;
- a job peaks at its measured cold peak;
- together they must fit 95% of the plan's RAM.

Recordings beyond the hosted limits are refused with the reason and can be analyzed offline.

**D-098 (empty environment variables).** An empty `HVSR_*` variable counts as unset. A copied
`.env` with blank secrets or URLs therefore uses the defaults (for example, a random per-process
signing key and no public base URL). It never yields an empty key or URL.

**D-099 (gate state of BLOCKED requirements).** A requirement recorded as BLOCKED (an external
dependency missing) is reported as PASS for a gate run in which its tagged tests actually ran and
all passed. Skipped tests keep it BLOCKED. UNVALIDATED keeps its state whatever the tests do,
because passing mechanics do not validate the science.

**D-100 (byte-exact references are tied to one numerical code path).** NumPy and OpenBLAS pick
CPU-specific code when they load. On CPUs with AVX-512, NumPy uses its own vectorized `exp`,
`log`, and `power`, and OpenBLAS uses its SkylakeX kernels. Both round differently in the last bits
from the AVX2 path that produced the shipped banks, `docs/examples/`, and the performance counts.
GitHub assigns runners of both kinds, so the same commit passed on one CI run and failed on the next
(AC-S3-12, AC-S8-10, AC-S9-01).

Evidence, from 24 GitHub Actions jobs on 2026-10-21 (`docs/evidence/stage9/numeric-path-probe.json`):
- Runners without AVX-512 reproduce every reference byte for byte, as this development machine does.
  These were AMD EPYC 7763 machines and one EPYC 9V74 that did not expose AVX-512.
- AVX-512 runners do not. These were Intel Xeon 8370C and 6973P-C, and AMD EPYC 9V45 and 9V74. With
  NumPy capped at the AVX2 target and OpenBLAS forced to its Haswell kernels, they reproduce every
  reference byte for byte. Capping NumPy alone is not enough.
- On their native AVX-512 path, every value in the example analysis documents and CSV files stays
  within `TOLERANCE_POLICY`. The largest relative difference is 3.4e-13, in a window table. Window
  selections, counts, statuses, and every other non-numeric field are identical.
- Regenerating the banks on that path moves the raw SDOF bank by at most 7 ULP. The Konno-Ohmachi
  and MDOF banks move by up to 5.2e-8 in log amplitude. Their anchors are maxima located by a
  bounded optimizer, and a maximum can only be located to about the square root of machine
  precision. Shipped banks are loaded from their files and never regenerated during an analysis.

Decision:
- The byte-exact reproduction tests are defined on the reference path: x86-64 with
  `NPY_ENABLE_CPU_FEATURES=X86_V3` and `OPENBLAS_CORETYPE=Haswell`. `tests/conftest.py` sets
  both, before NumPy loads, on any CPU that supports them. No assertion or expected value changes.
- Scripts that write byte-exact references must run on the same path. On an AVX-512 machine, set
  the same two variables before running them.
- Hosted and CLI analyses run on the machine's native path. Replay compares them within
  `TOLERANCE_POLICY`, whose scope already excludes cross-platform bitwise identity (spec 12.1).
- CI prints each runner's CPU model and native code path before the gate.
- `scripts/numeric_path_probe.py` repeats the comparison on any machine.
