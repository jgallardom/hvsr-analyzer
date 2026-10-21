# SESAME diagnostic report: implementation notes

Implementation id: `sesame-d23.12-impl-v1` (`hvsr_core.sesame`).

## Source

SESAME European research project, WP12, Deliverable D23.12, *Guidelines for the implementation of
the H/V spectral ratio technique on ambient vibrations: measurements, processing and interpretation*,
December 2004. <http://sesame.geopsy.org/Papers/HV_User_Guidelines.pdf>

The PDF is not fetched at analysis time. No checksum is bundled because the agent that wrote this
implementation could not retrieve and verify a canonical copy. The persisted source record therefore
has `sha256: null` with an explicit note. A maintainer who holds a verified copy should add its
SHA-256 to `SESAME_SOURCE` in `sesame.py`.

## Scope: reporting only

The report is computed from an already finished analysis document: each selection's geometric-mean
curve, its window-to-window dispersion, and the per-window peak frequencies. It never feeds back into
window QC, selection, spectra, peak families, template scoring, or the bootstrap.

The `Evaluate SESAME criteria` option is excluded from the scientific configuration hash. Toggling it
reuses the cached science and only regenerates the report (tests AC-S6-03 and AC-S6-08).

Frequency scopes stay distinct:
- peak search uses the configured search band;
- SDOF scoring uses [fp/2, 2 fp] on the peak-aligned grid;
- SESAME checks read the already computed mean curve over [f0/4, 4 f0].

Widening the SESAME interval never changes template scoring (test AC-S6-04).

## Quantities

| Symbol | Implementation |
| --- | --- |
| A(f) | geometric mean H/V of the selection's original (unshifted) window spectra |
| A0, f0 | observed peak amplitude and frequency of the selection's mean curve in the peak family |
| sigma_A(f) | exp(sample std of ln H/V over the selected windows); a multiplicative factor equal to 10^sigma_logH/V |
| sigma_f | sample std (ddof = 1) of per-window peak frequencies inside the family band |
| nw | number of selected windows; all selection methods choose non-overlapping, equal-length windows |
| Lw | window length in seconds |

sigma_A is window dispersion, not the plotted confidence interval. The report never uses the CI.

## Checks

The three reliability checks (all must pass) are:
- R1: f0 > 10 / Lw.
- R2: nc = Lw · nw · f0 > 200.
- R3: sigma_A(f) < 2 for f0 > 0.5 Hz, or < 3 for f0 < 0.5 Hz, for all f in (0.5 f0, 2 f0).

Satisfying R2 alone is not a complete sample-size recommendation. The guideline's practical
recording-duration recommendations are separate and are not checked here.

The six clarity checks (at least 5 of 6 must pass) are:
- C1: some f- in [f0/4, f0] has A(f-) < A0/2.
- C2: some f+ in [f0, 4 f0] has A(f+) < A0/2.
- C3: A0 > 2.
- C4: the peaks of A(f)·sigma_A(f) and A(f)/sigma_A(f) lie within ±5 % of f0.
- C5: sigma_f < epsilon(f0).
- C6: sigma_A(f0) < theta(f0).

Threshold table (published values):

| f0 band (Hz) | epsilon(f0) | theta(f0) for sigma_A | log10 theta(f0) |
| --- | --- | --- | --- |
| < 0.2 | 0.25 f0 | 3.0 | 0.48 |
| 0.2 - 0.5 | 0.20 f0 | 2.5 | 0.40 |
| 0.5 - 1.0 | 0.15 f0 | 2.0 | 0.30 |
| 1.0 - 2.0 | 0.10 f0 | 1.78 | 0.25 |
| > 2.0 | 0.05 f0 | 1.58 | 0.20 |

## Implementation conventions

The source does not settle the following points. They are choices made by this implementation, not
rules attributed to SESAME. Where a choice was needed, the more conservative option was taken.

- **C-1, band boundaries.** The table gives bands such as 0.2-0.5 and 0.5-1.0 without saying which
  band owns the shared endpoint. Each boundary value (0.2, 0.5, 1.0, 2.0 Hz) belongs to the
  higher-frequency band, which has the stricter epsilon and theta. Example: f0 = 1.0 Hz uses epsilon
  = 0.10 f0 and the 1.0-2.0 Hz theta.
- **C-2, theta columns.** The sigma_A column and the log10 column differ by rounding (for example
  1.78 versus 10^0.25 = 1.7783). The applied threshold is the smaller of the two, and both are
  persisted. The applied values are, by band: 3.0, 2.5, 10^0.30 = 1.995, 10^0.25 = 1.778 and 1.58.
- **C-3, R3 at f0 = 0.5 Hz.** The source states the threshold 2 for f0 > 0.5 Hz and 3 for
  f0 < 0.5 Hz. At exactly 0.5 Hz the stricter value, 2, is applied.
- **C-4, comparisons.** Every inequality is implemented strictly as written ("<" or ">"). A value
  exactly at a threshold fails, and that includes the ±5 % of C4. Some rounded examples in the
  source appear to treat equality as passing; the strict reading is the conservative one.
- **C-5, sigma_f.** sigma_f uses only windows that have a strict local maximum inside the peak
  family band. It needs at least 2 such windows and at least 50 % of nw; otherwise C5 is
  NOT EVALUABLE. A window without a peak is never assigned a frequency.

Other implementation details:
- **C4 peak location.** The maxima of ln A ± ln sigma_A are searched inside the peak family band,
  with three-point parabolic refinement in ln f. The check needs at least 3 valid bins that have a
  dispersion value.
- **C6 at f0.** ln sigma_A is interpolated linearly in ln f between the two bracketing valid grid
  points.

## Coverage and NOT EVALUABLE

- **Existential checks (C1, C2).** These use closed intervals. A valid observed witness passes even
  if the interval is truncated or partly invalid. A fail needs the whole interval to be inside the
  computed grid with every bin valid. Otherwise the check is NOT EVALUABLE: a missing witness on a
  truncated interval cannot establish a fail.
- **Universal check (R3).** This uses an open interval. A single valid violating bin establishes a
  fail. A pass needs full coverage with every bin valid; otherwise the check is NOT EVALUABLE.
- **Reliability status.** `reliable` if all three checks pass, `not_reliable` if any fails, and
  otherwise `not_evaluable`.
- **Clarity status.** `clear` if at least 5 checks pass, `not_clear` if 5 passes are impossible
  even if every not-evaluable check passed, and otherwise `not_evaluable`. Reliability and clarity
  are reported separately and never combined.
- **Option unchecked.** The report stores `{"status": "not_requested"}` and makes no compliance
  claim.
- **No defensible peak.** This covers a flat curve, no plausible family, or no peak in the family.
  All nine checks are listed as not evaluable, and no numerical maximum is substituted as f0.

Each check stores its measured value, threshold, comparison operator, status, and reason. The results
page links to the source PDF; figures never explain the criteria.

## Tests

`tests/stage_06/test_sesame.py` covers values immediately below, at, and above every threshold and
band boundary, under the conventions above.
