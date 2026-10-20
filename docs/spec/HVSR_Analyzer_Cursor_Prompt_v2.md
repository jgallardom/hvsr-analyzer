# CURSOR AGENT PROMPT - HVSR Analyzer: research implementation

Specification version: 2.0 - 2026-10-20
Status: implementation instructions, not a claim that the application or its statistical methods have already been validated.

This is a complete replacement for the previous HVSR Analyzer prompt. Follow this document when requirements conflict with an older version. Preserve existing measurements; do not apply destructive storage migrations without explicit authorization.

## 0. Mission and non-negotiable distinctions

Build a functional, tested, minimal-maintenance, open-source web application for three-component ambient-vibration HVSR analysis. Deliver a GitHub-ready repository and a Render deployment blueprint. Implement the application, not just an architecture proposal.

Treat peak-aligned SDOF window selection as an ongoing research method. Preserve R-squared as its ranking criterion. Do not replace the requested algorithm with conventional selection, but calculate a conventional reference as well. Scientific uncertainty and failed hypotheses must be visible, not hidden behind a polished interface.

Keep these concepts separate:

- Original uploaded bytes; a canonical numerical recording; derived analysis; a rendered plot.
- Observed spectral peak; template oscillator frequency; interpreted site fundamental frequency.
- Time-domain recording usability; peak identifiability; sensitivity to selection and processing.
- Precision of the selected-window result; uncertainty about peak identity, instrument validity, and geological interpretation.
- Computational reproducibility with identical inputs; robustness to different legitimate parameter sets; repeatability with a new recording.

Always attempt both Standard and SDOF-assisted processing for each scientific run. Optional 2DOF/3DOF template families are additional research methods, not replacements for SDOF. Do not fabricate a successful result when a method is not computable.

Main decisions in this revision:

1. Add sensor/response and measurement-context information.
2. Match precomputed templates, peak to peak, on a fixed log-relative-frequency grid. No online nonlinear regression, free peak shifting, or amplitude fitting.
3. Never frequency-align or amplitude-normalize the observed spectra used for the final HVSR average. Alignment is ONLY for scoring templates.
4. Plot one estimated 95% pointwise confidence band, with a clearly stated selected-window target and temporal-dependence treatment.
5. SESAME is reporting only: toggling it cannot change a spectrum or its window selection.
6. Remove the 1 MB recording cap. Introduce accountable monthly uploader quotas plus separate resource-safety limits.
7. Preserve small standard exports once; retain separate waveform, numerical-data, and plot downloads.
8. Support lossless canonical conversion, with an explicitly consented canonical-only option and no false claim of byte-for-byte original preservation.
9. Require acceptance-tested implementation stages and offline scientific validation.

## 1. Architecture and operating-cost priorities

Prefer Python, FastAPI, NumPy, SciPy, ObsPy, hvsrpy where suitable, SQLAlchemy, Pillow, Jinja2, and minimal JavaScript. Use a responsive, accessible interface. Plotly is acceptable for interaction; choose a lightweight, deterministic static exporter for PNG/SVG. Avoid a browser process per figure when a simpler scientific plotting library suffices.

Build the scientific core as a standalone Python package and CLI, independent of HTTP, credentials, databases, and Render. The same core must run on the researcher's Ubuntu computer and on the server.

Use managed persistent metadata storage and an S3-compatible object-store adapter. Cloudflare R2 Standard is an initial candidate, not a hard-coded dependency. Compare current official total costs with Backblaze B2 and one reasonable alternative before deployment. Consider database minimums, requests, downloads, backups, and compute, not only bytes stored. Provide explicit workload assumptions for 100, 1,000, and 10,000 measurements; do not pretend record count alone determines cost.

A single small Render application with bounded scientific execution is the preferred initial topology. Use a durable job table, atomic claims, leases, and recoverable status. An in-memory queue or a fire-and-forget HTTP background callback must not be the only record of a job. Add a separate worker only when profiling or deployment constraints justify it. Keep the scientific engine replaceable independently of hosting.

Never rely on ephemeral app storage, GitHub, or an expiring database tier for permanent measurements. Use environment variables for secrets and operational limits. Provide Dockerfile, render.yaml, dependency lockfile, .env.example, migrations, CI, README, health endpoints, backup and restore instructions, and cost/quota reporting.

## 2. Upload form, sensor metadata, and record identity

### 2.1 Recording inputs

Accept CSV, CSV.GZ, and supported MiniSEED variants. Support separate component files and multiple traces when their mapping is explicit. Accept optional StationXML separately. Declare supported MiniSEED versions/encodings after testing the pinned reader; never assume every version is supported.

Three synchronized components are required. Support common N/E/Z labels and manually mapped 1/2/Z channels. CSV templates must support elapsed time plus three components, UTC timestamps plus three components, and three components with an explicit sampling rate. Never silently infer ambiguous dates, units, component identities, or time zones.

Display sampling frequency, component names, start/end, duration, sample count, units, gaps, clipping, timing irregularities, and validation errors before running analysis. Preserve raw and analyzed time bounds. Do not silently interpolate gaps, resample irregular timestamps, or combine incompatible channels. Document any explicitly requested resampling/filtering as a derived operation.

### 2.2 Measurement and instrument fields

Collect latitude, longitude, wind level, traffic level, ground surface, observations, and optional sensor and area photographs. Wind/traffic include unknown as well as none/low/moderate/high. Keep surface material separate from measurement context.

Measurement context: free-field ground, building ground floor, building upper floor, basement, structure/other, or unknown. Capture floor/height and coupling/leveling notes when supplied. Do not automatically label an upper-floor peak as a free-field site fundamental frequency.

Sensor/recorder fields:

- Manufacturer/model; optional instrument identifier; sensor type.
- Recorded quantity and units: counts, velocity, acceleration, displacement, other, or unknown.
- Per-channel sensitivity/gain and units; digitizer gain if known; response epoch.
- Nominal sensor natural/corner frequency, when relevant, stored separately from a documented usable frequency band.
- Channel orientation, azimuth/dip if known, calibration/source reference, optional StationXML and its checksum.
- Whether instrument correction was applied, exact method, parameters, and response checksum.

Permit explicit unknown values without inventing calibration. Allow provisional analysis of compatible uncalibrated counts with a conspicuous instrument-response-not-verified flag. Do not divide physically incompatible channel quantities. Where responses/gains differ, require valid correction to a common physical quantity or mark the analysis invalid/provisional with the precise reason. Response removal cannot manufacture reliable data below the signal-to-noise limit.

Check the peak, the full template-comparison band, and SESAME diagnostic bands against usable response/signal coverage. Do not equate a nominal 4.5 Hz sensor corner with either universal validity or universal invalidity below 4.5 Hz.

For photographs: validate actual image contents and decompressed size; apply orientation; aspect-preserving resize/pad to exactly 300 x 300 pixels; strip EXIF including GPS; store only the processed image unless another policy is explicitly requested.

### 2.3 Record identity and correction history

Generate a standard full geohash from coordinates, retain it, and take characters 3 through 7 inclusive using one-based indexing. The human code is `ggggg-yyyy.yyyy`, where the second portion is UTC decimal year rounded to four decimals. Compute decimal year using the actual duration between consecutive January 1 timestamps, including leap years. Prefer recording start; otherwise use upload time and declare the source.

Use a UUID as the database identity. Partial geohash plus rounded decimal year is NOT inherently unique. Allocate collision suffixes `-02`, `-03`, etc. transactionally. Never overwrite. Human code examples must be labeled illustrative if not actual geohashes.

Permit versioned corrections to coordinates, sensor metadata, and observations. Keep the allocated record code stable, and retain the previous metadata snapshot referenced by each analysis. New parameter sets create new analysis runs, not new original measurements. Independently repeated acquisitions receive new measurement identities and may share a site/group ID.

## 3. Public sharing, licensing, uploader identity, and limits

### 3.1 Consent and attribution

Require unchecked explicit consent that waveforms, coordinates, processed photos, metadata, numerical results, and plots will be stored and publicly downloadable. State that only material the uploader is authorized to share may be submitted.

Use CC BY 4.0 for submitted data and applicable derived data/figures. Collect a public attribution name or organization, source citation, and rights confirmation. Store license URL/identifier, consent wording/version, timestamp, attribution, and modification history in manifests. Keep private quota identity/email separate from public attribution.

Explain that repository removal cannot recall already downloaded copies or revoke compliant recipients' CC BY rights. Provide a moderation/removal contact and workflow. Do not relicense third-party reference datasets, sensor documentation, or template banks without permission. Choose a separate software license consistent with dependencies; do not assume CC BY is a software license.

### 3.2 No 1 MB cap; enforce quotas honestly

Remove the previous 1 MB per-file/per-entry restriction. The primary user-facing budget is a configurable MONTHLY upload quota per stable contributor identity. An initial example is 1 GiB per contributor per UTC calendar month; label it an operational default, not a scientific standard, and expose it in deployment configuration.

Public reading/downloading remains anonymous. Reliable per-contributor quota enforcement requires a durable credential: use a managed passwordless identity or an administrator-issued upload token. Tokens can avoid a conventional account/password and be remembered by the browser. Do not claim that an IP address, a resettable cookie, or a self-issued unlimited anonymous token identifies a person. Optional guest uploads must be clearly documented as best-effort abuse-limited, not strictly per-person metering. Do not expose private credentials in public URLs.

Maintain an atomic quota ledger with UTC period, reservation, committed bytes, rollback, and idempotency key. Define upload charging explicitly: count original logical/uncompressed input bytes for waveform/CSV submissions so compression is not a quota bypass; meter stored bytes and incoming transfer bytes separately. Count complete accepted submissions, including associated uploads, under a documented policy. Retries must not be charged twice; deletions should not silently refund allowance and enable unlimited processing.

Also enforce separate configurable ceilings on request bytes, decoded bytes, decompression ratio, samples per channel, channels/files per submission, duration, candidate windows, bootstrap work, template comparisons, concurrent jobs, and per-job CPU/wall time. A configurable 128 MiB single-request ceiling is a reasonable initial safety example to benchmark, not a return to the 1 MB scientific limitation. Offer chunked/direct-to-object-store uploads when useful, with server-side validation and bounded staging.

Track reanalysis/compute usage separately from uploaded bytes. Estimate the cost of an analysis and optional sensitivity run before scheduling. When a budget is exceeded, explain whether to increase window step, reduce optional diagnostics, schedule offline processing, or request quota expansion. Do not silently shorten the recording, decimate it, or discard scientific outputs to meet a budget.

## 4. Canonical recording and economical preservation

### 4.1 Default: preserve originals cheaply

Preserve exact uploaded MiniSEED bytes. For CSV, an immutable gzip/zstd-compressed copy of the original bytes is acceptable: its original-file endpoint must reconstruct the exact original byte stream and SHA-256. Keep original-byte and stored-object checksums distinct. Compression is not data editing.

Create a canonical numerical recording only as useful for analysis, portability, or storage. Avoid gratuitous duplicates; use content-addressing internally, but keep separate provenance for separately submitted measurements. Do not deduplicate independent acquisitions merely because their locations match.

### 4.2 Optional canonical-only mode for future CSV uploads

Support an explicit opt-in policy to retain canonical MiniSEED plus complete metadata instead of original CSV bytes, ONLY after successful verification. This changes archival fidelity and must not be silent.

Conversion requirements:

- Parse and preserve sample values, sample ordering/count, component mapping, units, timing, rate, and all scientifically relevant extra fields in sidecars.
- STEIM is integer compression. Use it only for losslessly representable integer data supported by the chosen writer; no rounding/scaling floats to integers merely to compress.
- For floating values use a representation that preserves the declared parsed precision, normally FLOAT64. FLOAT64 MiniSEED is not inherently STEIM-compressed and may not be smaller than CSV.GZ. Compare actual sizes.
- Verify by reading the proposed archival representation with the production reader. Compare sample arrays bitwise at the declared canonical numeric precision, and validate exact supported time semantics/metadata. Do not call arbitrary decimal text to binary64 conversion byte-lossless; state the parsing contract.
- Preserve original timestamps separately when the selected MiniSEED format cannot represent them exactly. If irregular timing, precision, comments, extra scientific columns, or metadata cannot be retained reliably, refuse canonical-only conversion and preserve compressed original CSV.
- Persist original filename, format, byte size and SHA-256, conversion software/settings, canonical checksum, conversion report, and explicit consent. A checksum is not a substitute for retaining the discarded file.
- Do not delete staged CSV until canonical data, metadata, and verification are durably committed. Do not delete pre-existing archives as part of a routine upgrade.

When original CSV bytes are not retained, label downloads `Canonical recording` and `Reconstructed CSV`, never `Original CSV`. Publish `original_bytes_retained: false` and explain that formatting/comments not included in the preservation contract cannot be reconstructed. Exact-byte preservation remains the recommended default because storage savings may be small.

## 5. Deterministic scientific preprocessing

Parse, validate, align channels, apply the selected time range, correct instrument response where explicitly supported, remove mean, detrend as configured, taper, and optionally filter. Document exact operation order. Filtering is off by default. Changes that alter samples or response are versioned derived operations.

Initial settings: window length Lw = 50 s, candidate step dt = 5 s, peak search = 0.2 to 20 Hz, Konno-Ohmachi b = 40, horizontal RMS combination, and full recording interval. All are editable and are not universally suitable defaults. Recommend longer windows for lower frequencies. Warn that the strict SESAME inequality is not passed at exactly 0.2 Hz with exactly 50 s.

Use synchronized half-open sample-index windows; derive time bounds from integer indices, not cumulative floating-point addition. Keep actual FFT resolution separate from zero-padding/interpolated grid spacing. Validate Nyquist, smoothing support, and physical response coverage.

Compute reusable per-component Fourier amplitude spectra. Document the Konno-Ohmachi implementation, normalization, support, grid, and processing order. Default:

`H(f) = sqrt((N(f)^2 + E(f)^2) / 2)`

`HVSR(f) = H(f) / V(f)`

Support geometric horizontal combination as an explicit alternative. Use the same convention when comparing with hvsrpy/Geopsy. Guard small/invalid vertical amplitudes using documented numerical/noise-floor checks, not invented H/V values. Invalid bins are not zeros.

Keep a common physical frequency grid/band for final spectra within each run. Define an effective valid-frequency mask. Retain underlying component spectra or enough canonical data/cache information to audit narrow lines and denominator artifacts.

## 6. Peak discovery and fundamental-frequency interpretation

Do NOT define the fundamental frequency as simply the highest point anywhere in a curve. First identify plausible local peaks inside the search band, using physical coverage, prominence/width, recurrence, component-spectral evidence, and measurement context. Preserve a numerical global maximum separately.

Construct a shape-neutral discovery reference from quality-screened non-overlapping windows BEFORE SDOF ranking. Candidate recurrence cannot be inflated by counting many overlapping copies of one time interval. Discover recurrent peak families in log frequency; specify and version the clustering/prominence rules. Make thresholds research parameters, with null/contamination tests, not purported SESAME constants.

Associate each window with candidate peaks in these families. If no single family is defensible, analyze/report multiple plausible families rather than forcing all windows onto whichever template fits best. Identify a lowest plausible site-resonance candidate only where evidence supports it; the lowest visible bump is not automatically the fundamental. If lower frequencies are outside usable coverage, do not claim the observed lowest peak is the true first mode.

Check raw component spectra, very narrow persistent lines, harmonics, and smoothing sensitivity for potential machinery/source peaks. These are diagnostic evidence, not a rule that every narrow peak is artificial. Investigate different times/independent recordings when possible. Do not use additional SDOF correlations outside the allowed band for any of these checks.

Store separate fields:

- `maximum_amplitude`, `maximum_frequency_hz`.
- `observed_peak_frequency_hz`, `observed_peak_amplitude`, peak-family ID.
- `fundamental_frequency_candidate_hz`, interpretation status and evidence.
- Template peak, template undamped/modal frequency ratios, and equivalent scaled template frequencies, explicitly not direct site parameters.

Use statuses such as `single_plausible_peak`, `multiple_plausible_peaks`, `no_clear_peak`, `possible_source_peak`, `lower_band_unresolved`, and `not_computable`. A flat, usable record may correctly have no identified peak. A human interpretation is a separate attributed annotation and cannot overwrite calculated values.

## 7. Precomputed, peak-aligned SDOF matching

### 7.1 Match coordinates and fixed interval

For a window's candidate observed peak define `fp` and `Ap` using a fixed, reproducible peak estimator. Freeze them BEFORE evaluating damping templates. A small bounded interpolation of a sampled local peak is acceptable if documented; do not move fp or Ap to improve R-squared.

Use dimensionless relative frequency `q = f / fp` and `x = ln(q)`, not the logarithm of an isolated dimensional frequency. Evaluate a fixed odd-sized grid, initially 129 points uniformly spaced in x from `-ln(2)` to `ln(2)`, including q = 1. This balances the one-octave intervals below and above the peak. It is numerical comparison on a small grid, NOT regression against the full time series or nonlinear fitting.

Every candidate uses the complete relative interval q in [0.5, 2]. Its physical fitting interval is frozen at [fp/2, 2*fp]. Require actual valid positive amplitudes over that interval; no truncated fits, extrapolation, gap bridging, or omission of inconvenient bins. Interpolation must not overshoot or create peak structure.

All candidates within a comparison group must additionally cover the same predeclared common physical analysis band, including the union of the required per-window fit bands for that peak family. The relative comparison interval is identical; physical fit limits move only because different windows have different already-fixed fp. Do not claim those physical fit limits are identical. If an identical absolute band is requested in a future variant, version it as a different algorithm rather than silently changing this one.

A dense interpolation grid does not provide more independent spectral information. Check the actual FFT/smoothing resolution around the peak and minimum usable original-bin coverage. A fit lacking resolution is not rankable.

### 7.2 SDOF template definition and true peak alignment

Use a declared phenomenological template. Default: absolute mass displacement divided by harmonic base displacement for a linear SDOF oscillator:

`M(r, zeta) = sqrt((1 + (2*zeta*r)^2) / ((1-r^2)^2 + (2*zeta*r)^2))`, with `r = f/fn`.

The maximum of this damped transfer function is not exactly at its undamped natural frequency. For this specific unsmoothed template:

`r_peak(zeta) = sqrt(2 / (1 + sqrt(1 + 8*zeta^2)))`.

Normalize each template offline as:

`T_zeta(q) = M(q*r_peak, zeta) / M(r_peak, zeta)`.

Thus the designated peak is at q = 1 with amplitude 1. Align the comparison template by:

`predicted_HV(f) = Ap * T_zeta(f/fp)`.

No fitted vertical offset, free amplitude scaling, free natural-frequency optimization, or online damping regression is permitted in this algorithm. Enumerate a fixed library of damping shapes and select its best score. If an equivalent template fn is reported it is `fp/r_peak`, never a measured geotechnical damping/frequency estimate.

An initial small damping library may include 1%, 2%, 3%, 5%, 7.5%, 10%, 15%, 20%, and 30%. Treat this as a versioned experimental grid, not a validated universal range. Generate denser/different banks offline for validation, not by searching ever more templates until a match appears.

Template conditioning and smoothing must be explicit. A bank may contain raw templates and versioned effective/smoothed shapes for particular processing settings. Locate and normalize the actual peak AFTER any such conditioning. Do not double-smooth. Smoothing a theoretical H/V ratio is not generally identical to smoothing its underlying component spectra before division; document and benchmark that approximation. Fitted damping is a shape label, not a site damping measurement.

### 7.3 R-squared in log amplitude

For a window, form `y_j = ln(HV_window(fp*q_j)/Ap)`. For template k, use `t_kj = ln(T_k(q_j))`. Use equal quadrature weights on the uniform log-relative-frequency grid unless a different predeclared weighting is explicitly versioned.

`R2_log(k) = 1 - sum_j w_j*(y_j-t_kj)^2 / sum_j w_j*(y_j-y_bar)^2`.

This is the residual coefficient of determination, NOT squared Pearson correlation. It requires no regression fit. For each window/peak family, retain the template with the greatest valid R2_log and its full provenance.

Handle explicitly:

- Negative time-domain samples: normal and valid.
- Negative log amplitudes: normal when H/V or normalized H/V is below 1.
- Nonpositive spectral amplitudes: invalid for logarithms; diagnose rather than clip to invented values.
- Negative R-squared: retain its value, never clamp it to zero or square it.
- Zero/nearly zero weighted log-amplitude variance: R-squared is undefined/unstable; mark unrankable rather than returning 1.
- Nonfinite template values, unresolved peaks, incomplete bands, ties, and grid-boundary maxima.

Return `no_meaningful_match` when appropriate. There is no universal calibrated R-squared cutoff. Initially require at least a positive residual R-squared to label any candidate a provisional better-than-constant match, with stronger eligibility thresholds supplied by an offline calibration profile if available. Store raw scores even for rejected candidates. Do not fill a requested window count using poor matches merely to obtain a result.

Precompute template log arrays, norms, weights, and peak metadata. Batch comparisons using matrix operations, e.g. SSE from squared norms and dot products, with stable numerical handling. Score all windows against the same bank/version and grid. Do not perform SDOF comparison outside [0.5*fp, 2*fp], including ranking penalties or diagnostics.

### 7.4 Optional 2DOF/3DOF libraries

Implement an offline generator and safe import format for linear 2DOF and 3DOF shear-chain systems under common base excitation. Compare ONLY the absolute motion of the top mass relative to base motion, using the same type of transfer amplitude as the SDOF reference. Do not silently mix force-to-displacement, relative displacement, velocity, and absolute-motion transfer functions.

For relative coordinates U and harmonic base displacement Y:

`D(omega) = K - omega^2*M + i*omega*C`

`U/Y = solve(D(omega), omega^2*M*ones)`

`H_top(omega) = abs(1 + e_top.T @ (U/Y))`.

Support declared modal damping and/or Rayleigh `C = alpha*M + beta*K`, with modal ratios `zeta_i = alpha/(2*omega_i) + beta*omega_i/2`. Generate physically admissible finite grids of mass/stiffness ratios and damping OFFLINE. Check positive masses/stiffness, stability, and actual modal damping. Preserve dimensionless damping when frequency-scaling; do not scale stiffness while leaving damping inconsistent.

Designate the first-mode-associated local peak as the default anchor, not automatically the largest top-mass peak. Store its modal association, location, amplitude, and all other modal frequencies. Normalize that designated peak to q = 1 and amplitude 1. Ambiguous/unresolved anchor modes cannot be silently labeled fundamental.

Online work is interpolation/scaling and log-R-squared lookup only. Bank manifests include model family, excitation/output definitions, matrices or generating ratios, damping convention, modal ratios, anchor mode, grid, conditioning, generator version, license, and hashes. Upload data only; reject executable objects, pickle, and unsafe archive paths. Use NPZ with `allow_pickle=False` or a similarly safe array format, plus JSON Schema.

Report SDOF/2DOF/3DOF scores and results separately. Do not automatically choose a more flexible family because it obtains a larger best-of-many R-squared. Freeze library size/composition for a study and calibrate each family on independent/null cases. Where higher modes lie outside the allowed band, acknowledge that 1DOF/2DOF/3DOF may be indistinguishable; never extend the correlation band to identify them.

## 8. Window selection and research controls

### 8.1 Standard reference

Use a named, deterministic set of waveform-quality and stationarity checks, followed by non-overlapping window selection without a preferred oscillator shape. Specify every threshold and which established implementation is used. Separate basic clipping/gap/transient checks from optional frequency-outlier rejection, since the latter can also alter a peak distribution. Keep an audit reference using only basic quality screening.

### 8.2 SDOF ranking remains the requested rule

Apply the same basic invalid-data checks to both methods. Within each fixed peak family, rank eligible windows by their best SDOF R2_log. Choose the best remaining window; remove all candidates sharing any samples with it; repeat. Use half-open integer sample intervals and deterministic tie-breaking by original start index then stable ID.

Default automatic count: use all windows surviving the greedy procedure and predeclared match eligibility, subject to explicit operational limits. Never optimize n to minimize CI width, improve SESAME, maximize a quality score, or manufacture a sharper peak. Manual top-n mode is allowed and is labeled selection-conditional; insufficient matches remain insufficient.

Any research variant with a fixed acceptance fraction, per-block top-n, or minimum coverage must have its own ID and frozen parameters. R-squared remains the ranking rule within the eligible pool. Do not quietly replace the global greedy algorithm with another optimizer or a temporal quota.

The selected-window mean is an unweighted geometric mean of the ORIGINAL observed spectra on the common physical Hz grid. Do not weight by R-squared by default. In particular, do not align observed peaks, average dimensionless q spectra, normalize their peaks to 1, or reconstruct the final curve from winning templates. Those would suppress the frequency/amplitude variation the analysis needs to measure.

### 8.3 Reduce selection bias without abandoning the research method

Freeze bank, candidate rules, peak-family association, R-squared domain/grid, match threshold, selection/count rule, and planned robustness checks before examining the confirmatory output. Never tune them against the desired peak or confidence-band width.

Provide an optional research-validation workflow with temporally separated discovery and validation blocks, guard intervals preventing shared samples and short-range dependence, and a recorded split seed/protocol. Determine candidate-family interpretation and tune thresholds on development/discovery data only. Apply the frozen algorithm independently to validation data. The validation data may still use the fixed R-squared selector, so this measures repeatability of the selection algorithm; it does NOT magically remove all selection bias or establish the true site frequency.

Compare independent time groups, new recordings when available, Standard reference, and predeclared changes in candidate step and window count. Store inclusion frequencies/selection stability, but do not automatically promote whichever method gives the narrowest band. Do not reuse the held-out set for repeated tuning without labeling it development data.

Offline benchmarks must include more than signals generated by the very templates being fitted: flat/no-resonance cases, broad/asymmetric peaks, multiple peaks, a weak fundamental plus a stronger higher-frequency peak, colored/correlated noise, clean windows mixed with strong transients, persistent machine tones, component response mismatch, and reference layered/site-response cases when appropriately sourced. Evaluate both false-peak discoveries and bias/coverage on known targets.

Report how many windows failed and why as data-yield/context information. Do not apply a confidence penalty merely because many other windows failed. What matters for the selected result is the quantity, dependence, information, and reproducibility of accepted evidence, together with the remaining possibility of selection/model bias.

## 9. One uncertainty band, temporal dependence, and honest scope

### 9.1 Explicit estimand

The plotted curve estimates the frequency-by-frequency geometric mean of acceptable observed windows under the DECLARED selection policy and recording conditions. It is not the mean of every noisy interval, an unconditional site transfer function, or a prediction interval for a future window.

Use the label `Estimated 95% pointwise CI for the selected-window mean`. The page/JSON must say it is selection-conditional, identify the bootstrap/dependence method, and state whether coverage has been calibrated. Calling a bootstrap selection-conditional does not itself guarantee valid post-selection 95% coverage. Treat 95% as nominal until the stated target has been evaluated with held-out simulations/repeated data.

Use only ONE shaded band in a main plot. Do not also draw standard-deviation envelopes. Compute required dispersion internally for SESAME and diagnostics. Do not merge parameter-sensitivity spread or competing-peak uncertainty into this band and still call it a 95% CI.

### 9.2 Dependence-aware resampling of retained evidence

Resample whole observed spectra, never individual frequency bins. Average in log amplitude and back-transform. Preserve cross-frequency structure. Do not assume non-overlap means independence, and do not manufacture sample size by using all overlapping candidates.

Implement temporal block resampling:

1. Keep accepted windows' actual source timestamps/sample indices and acquisition/session identities. Never compress gaps away and then treat the remaining array as a uniformly sampled time series.
2. For sufficiently regular retained sequences, use a tested moving/stationary-block bootstrap with a documented block-length estimator. Assess dependence using log amplitudes around the peak and other predeclared spectral features, not only fp.
3. For irregular/sparse retained sequences, use original-time blocks containing whole accepted spectra, with boundaries/length chosen without minimizing the resulting CI. Resample blocks/clusters and account for their variable window counts in the mean. Do not apply a regular-grid automatic block-length formula to gap-compressed data. Avoid false stationarity across different acquisition sessions.
4. Empty/rejected periods contribute no artificial spectral values. Their count does not inflate the band. Preserve timing information so accepted evidence clustered in one episode is not counted as many independent observations.
5. If adjacent blocks remain dependent, increase/model the dependence span or use a validated block-resampling variant. Do not claim a universal block size works for all recordings. Record the estimator, measured dependence diagnostics, block length in seconds, block count, and limitations.
6. Assess a few predeclared reasonable block lengths; report sensitivity, not the narrowest answer. If there are too few informative blocks to estimate dependence/uncertainty, return `ci_not_estimable` or `ci_limited` instead of a zero-width or spuriously confident interval. An initial guard of 10 informative blocks is an experimental operational threshold to validate, not a SESAME rule or proof of coverage.

Use a recorded deterministic seed and bounded bootstrap count, initially 2,000 for final results if resource tests permit. Cache candidate spectra; bootstrapping must not repeatedly parse files or recompute FFTs. Quantiles at 2.5% and 97.5% give nominal pointwise bands. Effective-sample-size estimates, where justified, are diagnostics; do not apply an unexplained second inflation to an already dependence-aware bootstrap.

Peak uncertainty: for each replicate recompute the observed mean spectrum and its peak within the predeclared family/band. Do not realign peaks across replicates. Track no-peak and alternate-family outcomes instead of silently dropping them. A multimodal peak distribution must be reported as ambiguous; a conditional interval for one family must state its conditioning and no-peak/other-family frequencies.

### 9.3 What the band does not guarantee

A high rejection rate alone must not widen a band for a well-supported retained regime. Conversely, many very similar windows from one persistent machine tone or one correlated episode cannot establish a true site resonance. The curve may be precise conditional on the selected evidence while peak identity remains unresolved.

Sensor uncertainty, model mismatch, peak-family ambiguity, and selection sensitivity are separate reported dimensions unless a validated joint uncertainty model actually includes them. Do not imply the confidence band includes them by default.

Optional expensive validation, preferably offline: repeat the full fixed selection procedure inside a dependence-preserving simulation/resampling experiment to assess selection-algorithm variability. Do not assume ordinary bootstrap-after-selection or nested bootstrap automatically repairs discontinuous top-n selection. Evaluate empirical coverage and bias on independent test cases before making stronger claims.

## 10. Optional SESAME report, isolated from processing

The checkbox `Evaluate SESAME criteria` defaults on. It controls calculation/display of a diagnostic report only. With fixed scientific inputs, toggling it MUST leave candidate windows, selected windows, mean spectra, peak estimates, bootstrap seed/results, and template scores unchanged. Test this explicitly. A reporting-only change can create a versioned diagnostic artifact without duplicating the core spectral computation.

Implement the official three reliability checks and six peak-clarity checks from SESAME D23.12, with correct log/multiplicative dispersion conventions, original thresholds, and the at-least-five-of-six clarity rule. Preserve per-method/per-peak measured values, thresholds, status, and reasons. Do not confuse standard deviation with the plotted CI.

Reliability includes `fp > 10/Lw`, significant cycles `Lw*nw*fp > 200` for selected non-overlapping equal-length windows, and the specified dispersion condition. The guideline also gives practical recording/window recommendations; satisfying the cycle inequality alone is not a complete sample-size recommendation.

Frequency scopes are distinct: peak search, SDOF scoring [fp/2, 2*fp], and SESAME checks that can require [fp/4, 4*fp]. Compute wider observed spectra only for those scientific checks/diagnostics; do not extend SDOF matching. Where required coverage is missing, use NOT EVALUABLE. For existential tests, a valid observed witness can establish a pass; absence of a witness on a truncated interval cannot establish a fail for the full interval.

The source contains strict-inequality versus rounded-example inconsistencies and ambiguous exact frequency-bin boundaries. Document the chosen boundary convention in `docs/sesame-implementation.md`; label implementation conventions rather than falsely attributing them to an unambiguous original rule. Prefer clearly declared conservative handling at unresolved boundaries. Include unit tests immediately below, at, and above every boundary. Persist the source version/URL/checksum where available; do not fetch the PDF on every analysis.

When unchecked store `not_requested` and make no SESAME compliance claim. When no defensible peak exists, do not force a numerical maximum into a fundamental-frequency claim; report which peak-dependent checks are not evaluable. Keep reliable-curve status separate from clear-peak status. On the results page link to the source PDF; never explain SESAME criteria in the figure itself.

## 11. Separate usability, peak identifiability, and sensitivity

Display distinct diagnostic panels:

- Recording/retained-data usability: valid channels, clipping, duration, gaps, coverage, response status, available non-overlapping evidence and temporal groups.
- Peak identifiability: prominence, recurrence, fp/Ap variation, plausible competing families, and source-artifact evidence.
- Selection/processing sensitivity: Standard versus SDOF, optional model families, window count/step, parameter runs, and time-group checks.

An optional experimental 0-100 summary must expose its formula, weights, component values, missing inputs, and version. It is not a probability of correctness, not SESAME-endorsed, and not grounds to force a method winner. Do not penalize recording quality merely for a flat curve or merely because SDOF and Standard disagree.

There is NO target post-peak amplitude of 0.5, no preferred absolute valley level, and no such configurable option. Keep ordinary peak prominence or SESAME half-peak criteria separate from that removed idea. High R-squared is template resemblance, not a calibrated quality probability. A narrow confidence band alone cannot make SDOF the preferred scientific result.

## 12. Reproducibility and planned parameter sensitivity

### 12.1 Identical-input replay

Persist original/canonical hashes, metadata revision, response hash, complete explicit parameters, window sample indices, frequency arrays, template-bank hash/version, algorithm versions, seed, dependency lock, git commit (plus dirty diff hash if needed), container/image identity, and relevant numeric-backend details.

Define a canonical scientific configuration hash excluding purely visual and SESAME-display options. Identical scientific requests reuse an immutable cached result by default; an explicit verification rerun bypasses the cache and compares actual computations. Do not pretend loading a saved CSV is a computational reproducibility test.

Same data/settings/versions/seed must reproduce numeric arrays and selection within predeclared numerical tolerances. Do not promise cross-platform bitwise identity without testing. Stabilize reductions and tie-breaking; record tolerance policy. Plot bytes may additionally depend on fonts/renderers/timestamps; preserve published plot bytes rather than assuming future rerenders are identical.

### 12.2 Two parameter sets test robustness, not exact identity

Provide a `Run robustness check` option, initially on when resource budgets/data allow. Before examining results, define two related runs:

- A: the user's primary Lw, b, dt, analyzed time span, and other settings.
- B: 1.5*Lw rounded to a valid sample count, with b, dt, physical search band, time span, component combination, and response settings unchanged.

For the initial 50 s default, B uses 75 s. If B has insufficient data/full-band coverage, report not evaluable; do not search for a different B that agrees. A third optional cheaper smoothing-only check changes b under a predeclared plan while reusing FFTs. Do not change many parameters at once by default.

Both Standard and SDOF are attempted within each run. Store each run independently and link them through a robustness-comparison record. Compare peak-family identity, fp, Ap, peak width, spectra on common supported frequencies, window selection, and uncertainty status. Report relative differences plus actual FFT/effective resolution. Do not require identical amplitudes or point-by-point spectra from different window lengths/smoothing.

Agreement tolerances are prespecified research criteria tested on known signals, not universal SESAME rules. CI overlap alone is not an equivalence test. Runs on the same waveform are correlated and cannot be counted as independent repeated measurements. Do not average them as independent samples or pick the prettier/narrower result. Where ambiguity or strong sensitivity persists, show it.

## 13. Public views, saved exports, and AI-friendly access

### 13.1 Views

Provide Upload/Records, Configuration, and Results views, plus public search and analysis history. Keep English strings centralized for later Spanish localization. The principal results title is `RECORD_CODE (latitude, longitude)`.

Configuration includes Lw, dt, peak-search limits, analyzed tmin/tmax, smoothing, component combination, preprocessing, template library, automatic/manual n, bootstrap/dependence settings, SESAME checkbox, and planned robustness check. Separate scientific settings from display settings and admin resource limits.

Main plot: linear frequency in Hz on x, logarithmic positive H/V on y; original observed mean; ONE nominal 95% pointwise CI if estimable; observed main-peak marker and separate maximum if different. Optional individual windows or alternate method can be toggled. Do not substitute a theoretical fitted curve for the data.

Plot provenance footer: original record code, lat/lon, immutable run ID, selected method/peak family, Lw/dt/n, smoothing/b, search and time limits, component combination, relevant preprocessing, instrument-status shorthand, processing version, template-bank ID when used, CI scope/method and effective block count or availability. Keep full details in JSON and a stable run URL. Avoid overcrowding. No SESAME criteria explanations inside the figure.

### 13.2 Persist small outputs once

At successful run completion generate and durably save canonical numerical arrays and the standard CSV, JSON, PNG, and SVG exports for each method. These are immutable and served directly/through stable redirects. Generate uncommon diagnostics, alternate image sizes, or bespoke bundles on demand; cache when useful.

A cosmetic plot change creates a new plot artifact referencing the existing numerical run, not another FFT/bootstrap run. Store figure recipe and rendering version. Plot selection never alters the scientific data.

Keep separate logical object categories in one bucket unless a measured requirement justifies more: recordings, sensor/metadata, numerical analyses, plots, and template banks. Separation means independent objects/downloads, not separate servers. Optional ZIP bundles do not replace individual endpoints.

### 13.3 Machine-readable contracts

Publish a measurement `manifest.json` and per-run/method `analysis.json`, with stable JSON Schemas, units, UTC ISO 8601 timestamps, schema versions, explicit status enums and null semantics. No NaN/Infinity tokens in JSON and no JSON encoded inside string fields. Numeric CSV is UTF-8 with decimal points and explicit headers/units. Represent unavailable numbers as blank/null plus reason, not zero.

Manifest categories: original_data (if retained), canonical_recordings, sensor_metadata, analysis_runs, spectra, plots, templates, provenance, licensing, and quality_evaluation. Each artifact has stable API URL, MIME type, byte size, checksum, generation/version info, availability, method, run, and exact source references. Do not embed expiring signed-storage URLs as permanent manifest links; stable endpoints may issue fresh redirects.

Required independently retrievable products:

- Each retained original file, clearly distinguished from canonical and reconstructed data.
- Canonical waveform and optional normalized CSV/CSV.GZ export, without requiring an AI to decode MiniSEED or inspect an image.
- Mean-spectrum CSV with frequency_hz, hv_geometric_mean, ci95_lower/upper, count, and validity/status as needed.
- Per-window metadata/ranking CSV/JSON and downloadable per-window numerical spectra. Preserve selected AND rejected candidate provenance; retain compact candidate spectra or support deterministic, resource-bounded regeneration with explicit availability metadata.
- Per-run/method analysis JSON with fp/Ap statistics, exact grid, template definitions/scores, selection/uncertainty policies, SESAME if requested, calibration status, source hashes, and run parameters.
- Separate PNG/SVG plot downloads, template-bank data, and optional comparison reports.

Never require image OCR to recover numerical values. Include a data dictionary, concrete example exports, schemas, citation guidance, and license/attribution fields. Distinguish measured data, algorithmic results, experimental diagnostics, and human interpretation explicitly. Expose AI-readable HTTP endpoints without requiring a server-side LLM.

## 14. Efficient computation and safe offline precomputation

Prioritize algorithms that are intrinsically cheap, not merely timeouts around slow implementations.

- Parse/validate once; process bounded chunks; avoid copying full arrays per candidate.
- Share preprocessing/FFT/component spectra between Standard and SDOF, SESAME diagnostics, and compatible parameter runs. Cache by exact scientific inputs.
- Build or cache smoothing weights/operators by sample rate, Lw, grid, b, and normalization. Avoid unbounded dense O(F^2) matrices; validate any sparse/truncated approximation against a trusted reference and record tolerance.
- Use vectorized fixed-grid template comparisons; precompute log arrays and norms. Bound windows x templates x grid work and batch sizes.
- Bootstrap cached spectral vectors with efficient weight/count operations. Do not recompute FFTs or evaluate differential equations for each bootstrap replicate.
- Generate all default plot formats once. Serve large immutable bytes from object storage, not repeatedly through Python.
- Define an acyclic processing dependency graph so reporting or presentation changes reuse existing science.

Offline CLI deliverables must include generation/validation of SDOF, 2DOF and 3DOF banks; optional smoothing/operator caches; Monte Carlo bias/coverage experiments; and local replay of complete analyses. Output portable data plus signed/hash-verified manifests. Precompute model-dependent quantities offline, not data-dependent fp or actual record statistics. SDOF lookup remains available even without optional larger banks.

Only authorized administrators may upload/activate template banks. Validate schemas, types, sizes, hashes, numerical domains, matrix dimensions, and license. Keep old versions for replay. Never execute uploaded Python, pickle, or arbitrary serialized objects. Externally computed analysis results, if imported, are marked external and validated; they cannot masquerade as server-verified results without verification.

Profile representative small/medium/large recordings on the chosen service size. Store wall time, peak memory, candidate count, template comparisons, bootstrap count, and artifact bytes. Set budgets from measured behavior, not invented speed claims. Prevent oversubscribed numerical threads and runaway concurrent jobs. Jobs must survive/recover cleanly from interrupted workers; partial scientific output is not a completed run.

## 15. Acceptance-tested implementation stages

### 15.1 Mandatory agent working protocol

Create `AGENTS.md`, `docs/implementation-status.md`, `docs/scientific-decisions.md`, `docs/acceptance-matrix.md`, and `artifacts/test-reports/` at the start. Translate every SHALL/MUST requirement into traceable acceptance IDs. Use small stages; implement tests first where practical, then code, then execute the tests.

For every stage:

1. State the stage goal and acceptance IDs in the work log.
2. Implement the smallest working vertical slice; do not use mock scientific values.
3. Execute the stage tests AND all previously passing regression tests.
4. Record exact commands, environment, exit status, elapsed time, and JUnit/test outputs. Save numerical reference comparisons and benchmarks where applicable.
5. Fix failures before proceeding. Do not weaken assertions, change expected results, loosen scientific tolerances, or exclude failing scenarios just to pass. Any justified change must be documented with evidence.
6. Commit/checkpoint the passing stage when repository operations are available. Update the next action so a later agent session can resume without guessing.
7. If credentials, external fixtures, or scientific calibration are unavailable, mark the affected gate BLOCKED or UNVALIDATED and state what is needed. Unrelated safe work may continue, but do not call the stage passed or the result production-validated.

Implement an executable `scripts/gate.py --stage N` (or equivalent) that runs the appropriate checks and fails with a nonzero exit code. Have CI call the same gates used locally. Example underlying command: `python -m pytest tests/stage_03 tests/regression --junitxml=artifacts/test-reports/stage_03.xml`. Create the actual test paths before documenting this as runnable.

### Stage 0 - Contracts, numerical conventions, and fixtures

Deliver schemas, scientific API, configuration model, units/timing rules, package layout, immutable run contract, and versioned decision log. Pin compatible dependencies after checking official documentation. Create synthetic/reference fixtures with known properties and legal redistribution status.

Acceptance: schemas validate examples and reject malformed data; no undocumented defaults; all parameters affecting a result are serializable; a clean local environment can import the core without network/cloud credentials; every later stage has acceptance IDs.

### Stage 1 - Inputs, sensor checks, and preservation

Implement waveform parsing, alignment, metadata/versioning, response-status checks, hashes, CSV.GZ preservation, and optional verified canonical conversion.

Acceptance: valid CSV/MiniSEED; missing/ambiguous components; differing gains/units; gaps/irregular time; unsupported encodings; unknown response; exact original reconstruction; float64 round-trip; integer STEIM round-trip; failed conversion never deletes source; canonical-only labels truthful; compressed-bomb rejection. Include nonrepresentable timestamps and extra CSV columns.

### Stage 2 - Standard HVSR scientific core

Implement preprocessing, windows, FFT, smoothing, component combination, shape-neutral screening, observed peak families, and output arrays.

Acceptance: compare against hvsrpy/Geopsy under matched settings using independently established fixtures; test flat curves, clean/broad/multiple peaks, weak low peak plus strong higher peak, clipping, zero/invalid vertical bins, timing endpoints, and frequency coverage. Document tolerance from numerical/method differences, not an unexplained large allowance.

### Stage 3 - Offline template generation and R-squared selector

Implement finite SDOF bank, actual-peak normalization, fixed q grid, log score, deterministic greedy selection, bank manifests, and optional 2DOF/3DOF generator/import.

Acceptance: designated peaks align at q=1 after normalization; dense direct SDOF agrees with lookup; known templates score near 1 within declared interpolation tolerance; damped peak is not confused with fn; log-frequency symmetry; incomplete bands rejected; no outside-band scores; negative R-squared retained; flat denominators undefined; manual n never fills with invalid matches; overlap/ties exact; switching bank versions changes provenance; 2DOF/3DOF top-motion response agrees with independent matrix solves and modal/low-frequency limits.

CRITICAL TEST: windows with peaks at different absolute frequencies are normalized only for matching. The final observed average and peak scatter retain their original frequencies and amplitudes. A test must fail if final spectra are peak-aligned or amplitude-normalized.

### Stage 4 - Statistical summaries and dependence-aware CI

Implement observed geometric mean, temporal block resampling, peak-bootstrap outcomes, interval status, and one-band export contract.

Acceptance: log-mean identities; deterministic seeds; spectral-vector resampling; clustered/correlated evidence does not acquire the iid sample count; gaps preserved; too-few-block behavior; no artificial zero-width confidence; alternate/no-peak replicates tracked. Holding accepted spectra/times fixed while adding rejected data outside them must not widen the CI merely via a rejection-rate factor. Test block sensitivity and distinguish conditional mean CI from window dispersion.

### Stage 5 - Offline selection-bias and coverage validation

Implement a reproducible Monte Carlo/repeated-record research harness with fixed development and locked test sets, declared truths/estimands, null cases, contamination, temporal correlation, and non-SDOF reference shapes. Compare Standard, SDOF, and optional bank families separately.

Report bias, absolute/relative fp error, false-peak rate, interval coverage and width, no-result rate, retained temporal evidence, and compute cost. Include cases where R-squared improves while the inferred fundamental becomes wrong. Distinguish coverage for the selected-regime mean from coverage for a known physical resonance.

Acceptance: predeclare simulation counts, seeds, metrics, and Monte Carlo acceptance ranges before checking final tests. Account for binomial/Monte Carlo error instead of requiring every nominal 95% interval to contain truth. Do not tune and validate on the same simulated seeds/scenarios. If nominal coverage/false-peak targets fail, publish the failure and keep the method labeled experimental/uncalibrated; do not advertise validated 95% site-frequency accuracy. Application mechanics can pass while a scientific-validation gate remains unvalidated.

### Stage 6 - SESAME isolation and robustness/replay

Implement official diagnostic calculations, explicit source-boundary conventions, exact-input replay, two-run comparison, separate quality dimensions, and human annotations.

Acceptance: all SESAME criteria with pass/fail/not-evaluable cases and threshold boundaries; checkbox on/off leaves all scientific arrays/selections identical; wider SESAME diagnostics do not extend template scoring; replay bypasses cache and reproduces numerics; 50/75 s clean synthetic results agree within predeclared test tolerances where adequately resolved; deliberately sensitive/multipeak cases are honestly flagged, not forced to agree; cosmetic changes do not invoke FFT/bootstrap.

### Stage 7 - Durable storage, quotas, licensing, and job execution

Implement object adapter, metadata migrations, artifact commits, stable URLs, contributor ledger, CC BY consent, moderation, and recoverable jobs.

Acceptance: concurrent quota reservations cannot exceed budget; retries not double-charged; no guest hard-per-person fiction; staged/failed uploads cleaned safely; immutable archives survive app restarts; expired signed URLs do not break permanent manifest endpoints; original/canonical labels remain correct; all provenance/license fields present; worker interruption/retry cannot publish partial or duplicate science. Demonstrate one backup restore.

### Stage 8 - Web interface and independent exports

Implement the three pages, history, configuration, photos, one-band plot, sensor/quality warnings, method/family toggles, public records, and independent downloads.

Acceptance: complete upload-to-result flow; current run/method matches plot/CSV/JSON exactly; separate original/numerical/plot downloads; normalized waveform CSV; optional missing originals disclosed; plots contain required provenance but no SESAME explanations; figures readable on desktop/mobile; license/attribution visible; each manifest link resolves or has an explicit unavailable state; schema validation on real exports.

### Stage 9 - Efficiency, security, and deployment handoff

Profile cached/uncached processing and both parameter runs; bound concurrency, templates, bootstrap, decoded uploads, and memory; complete GitHub CI and Render blueprint.

Acceptance: reproducible performance report on declared hardware; no unsupported speed claims; malformed input/bank tests; dependency/security scans; process-time limits and durable retries; warm-cache reporting does not rerun science; all mandatory gates executed; documented monthly cost assumptions and restore procedure; fresh Docker/local startup and Render configuration smoke tests.

Remote GitHub push or deployment requires real credentials. Do not claim either occurred unless verified. Deliver a locally working, deployment-ready repository even when remote operations are blocked.

## 16. Deliverables and references

Deliver the source, CLI, schemas, tested scientific core, template generator/importer and sample safe banks, Standard/SDOF outputs, optional 2DOF/3DOF support, offline research harness, web UI, persistent artifact storage, quotas/license handling, independent exports, immutable history, tests/reports, GitHub/Render configuration, and honest scientific-validation status.

Documentation must explain how to generate expensive banks/benchmarks on the researcher's computer, upload only the resulting validated data files, activate a bank version, replay an analysis offline, and resume development from the next failing/unimplemented acceptance stage. Never substitute a plausible-looking mocked spectrum for a computation.

Primary technical references to verify and cite in project documentation:

- SESAME D23.12, Guidelines for the Implementation of the H/V Spectral Ratio Technique on Ambient Vibrations, December 2004 document with 17/03/05 page headers: https://sesame.geopsy.org/Papers/HV_User_Guidelines.pdf
- hvsrpy API and implemented-method references: https://hvsrpy.readthedocs.io/en/stable/ and https://hvsrpy.readthedocs.io/en/stable/window_rejection.html
- FDSN StationXML response metadata: https://docs.fdsn.org/projects/stationxml/en/latest/response.html
- FDSN MiniSEED encodings: https://docs.fdsn.org/projects/miniseed3/en/latest/data-encodings.html ; verify actual writer/version compatibility separately.
- ObsPy MiniSEED support: https://docs.obspy.org/packages/obspy.io.mseed.html
- R-squared edge cases: https://scikit-learn.org/stable/modules/generated/sklearn.metrics.r2_score.html
- Time-series block bootstrap and block-length selection: https://bashtage.github.io/arch/bootstrap/timeseries-bootstraps.html and https://bashtage.github.io/arch/bootstrap/generated/arch.bootstrap.optimal_block_length.html
- Fithian, Sun, Taylor, Optimal Inference After Model Selection: https://arxiv.org/abs/1410.2597 . This motivates careful post-selection claims; it does not directly validate the proposed HVSR bootstrap.
- Rayleigh damping convention: https://opensees.github.io/OpenSeesDocumentation/user/manual/model/damping/rayleigh.html
- CC BY 4.0: https://creativecommons.org/licenses/by/4.0/ and its legal code.
- Current hosting/storage terms: https://developers.cloudflare.com/r2/pricing/ and https://render.com/docs/free

These sources do not endorse the proposed template-selection algorithm or its experimental quality scores. Label original project design decisions separately from established methods. Begin at Stage 0, implement, execute acceptance gates, and continue without declaring untested work complete.
