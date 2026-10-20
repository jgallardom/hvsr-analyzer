# Template banks and the R-squared selector

Algorithms: `sdof-r2log-q129-v1` (matching), `sdof-greedy-nonoverlap-v1` (selection).
Code: `hvsr_core.templates`, `hvsr_core.mdof`, `hvsr_core.matching`. Tests: `tests/stage_03/`.

## Fixed comparison grid

`x_j = (j - 64) * ln(2) / 64`, `j = 0..128`, `q_j = exp(x_j)`: 129 points uniform in `ln q` over
`[-ln 2, ln 2]`, exactly symmetric, `q_64 = 1`. Equal quadrature weights `w_j = 1/129`.

## SDOF templates

`M(r, zeta) = sqrt((1 + (2 zeta r)^2) / ((1 - r^2)^2 + (2 zeta r)^2))`, `r = f / fn` (absolute mass
displacement over harmonic base displacement). Raw peak `r_peak = sqrt(2 / (1 + sqrt(1 + 8 zeta^2)))`
(< 1 for every zeta > 0). Template `T(q) = M(q r_peak) / M(r_peak)`: peak at q = 1, amplitude 1.
The reported equivalent template frequency is `fp / r_peak`. It is a shape-scaled number, NOT a
measured site or structural natural frequency, and the damping label is NOT a damping measurement.

Damping grid (versioned, experimental): 1, 2, 3, 5, 7.5, 10, 15, 20, 30 %.

## Conditioning (D-042)

The pipeline smooths H and V with Konno-Ohmachi on linear-frequency FFT bins and then divides. The
conditioned banks apply the continuous limit of that operator to the theoretical ratio:

`T_b(r) = int W(u) M(r e^u) e^u du / int W(u) e^u du`, `W(u) = [sin(b u/ln10) / (b u/ln10)]^4`,
`|b u / ln10| <= 3` (trapezoid rule, 4001 nodes; agrees with 16001 nodes to < 1e-6 in ln).

The peak is located after smoothing (bounded scalar search) and normalized there, so the conditioned
anchor differs from the raw `r_peak`. Smoothing is applied once (to the template only).

Benchmark (`tests/stage_03/test_conditioning_benchmark.py`, output
`artifacts/benchmarks/template_conditioning.json`): noise-free ratios processed exactly as the
pipeline does (component smoothing on FFT bins with Lw = 50 s, then division), fn in {0.6, 2, 8} Hz,
all 9 damping values, flat and f^-1 base spectra:

| Bank | Best R2_log range | Correct damping label |
|---|---|---|
| `sdof-ko40-v1` | 0.9887 - 1.0000 | 54 / 54 cases |
| `sdof-raw-v1` | 0.9388 - 0.9979 | 24 / 54 cases (biased toward higher damping) |

The worst conditioned case (fn = 0.6 Hz, 1 %) reflects discretization: few FFT bins under the
smoothing window. A colored base spectrum changes these numbers by < 1e-4 for SDOF shapes; for
sharply varying base spectra the approximation can be worse and is not covered by this benchmark.
`template_bank = "auto"` therefore uses the conditioned bank matching the run's b
(shipped for b in {20, 30, 40, 50, 60, 80, 100}); other b values fall back to the raw bank with a
recorded warning.

## Interpolation tolerance (AC-S3-03)

Noise-free windows built from a template on the 0.005-decade analysis grid, with the true peak swept
over 100 sub-grid positions: worst `1 - R2` = 5.6e-3 (raw 1 %), 1.4e-4 (raw 2 %), 1.5e-5 (raw 3 %),
<= 2.1e-6 (raw >= 5 %), <= 1e-6 (conditioned b = 40); the correct template is always selected.
Declared tolerance: 1e-2 for raw 1 %, 5e-4 otherwise. The raw 1 % peak (half-power width ~0.02 in
ln f) is narrower than two grid steps, so the grid limits how well it can be resolved.

## Matching rules

1. `fp`, `Ap`: maximum of the window's smoothed ln(H/V) inside the family bounds that is a strict
   local maximum of the full curve, refined by a 3-point parabola in (ln f, ln A), offset bounded to
   +/- half a grid step. Computed before, and independently of, any bank.
2. The window must be valid at every grid point bracketing `[fp/2, 2 fp]` (`incomplete_fit_band`),
   and at every grid point bracketing the family's common band `[f_lo/2, 2 f_hi]`
   (`outside_common_band`). The bracket includes at most one grid point (0.5 %) beyond each end,
   needed to interpolate at the exact endpoints. No extrapolation, truncation, or gap bridging.
3. At least 10 FFT bins in `[fp/2, 2 fp]` and 4 in `[fp/2, fp)` (`insufficient_resolution`).
4. `y_j = ln HV(fp q_j) - ln Ap` by linear interpolation of ln HV in ln f.
5. `R2_log = 1 - sum w (y - t)^2 / sum w (y - ybar)^2`, batched via norms and dot products; the best
   template's SSE is recomputed directly. Weighted variance < 1e-6 gives `undefined_r2`. Negative values
   are kept. Exact ties choose the first template in bank order and set `tie`; best templates at a
   parameter-grid edge set `grid_boundary_best`.
6. Eligible: `R2 > r2_min` (default 0, strictly better than a constant). Otherwise
   `no_meaningful_match`; raw scores are still stored. No calibrated cutoff exists.

Selection (per family): eligible windows by descending R2, then start index, then window ID; take the
best, discard every candidate sharing any sample (half-open intervals), repeat. Automatic mode keeps
all survivors. `manual_top_n` keeps the first n and reports `insufficient_matches` if fewer exist.

The selection mean is `curves.geometric_mean_curve` over the selected windows' ORIGINAL H/V on the
physical Hz grid, unweighted. `tests/stage_03/test_matching.py::test_CRITICAL_*` and its mutation
tests fail if that curve is peak-aligned or amplitude-normalized.

## 2DOF / 3DOF banks

Shear chains (masses `m_i/m_1`, springs `k_i/k_1`, base spring k_1), scaled so the first undamped
natural frequency is 1. `D(w) = K - w^2 M + i w C`; `U/Y = solve(D, w^2 M 1)`; output
`|1 + U_top/Y|` (absolute top displacement over base displacement). Damping: modal (declared ratios)
or Rayleigh `C = alpha M + beta K` with `zeta_i = alpha/(2 w_i) + beta w_i / 2`. Admissibility: positive
masses and stiffnesses, classical damping (modal C diagonal), declared = actual modal damping,
`0 < zeta_i < 1`, stable state matrix. Verified against an independent modal-superposition solution
(rtol 1e-10), the rigid-body limit, a 1-DOF chain (= SDOF formula), and a rigid 2-DOF (-> SDOF).

Anchor: the largest local maximum in `(0.3, sqrt(w1 w2))` after conditioning, which must lie within half
the log-distance to that midpoint. Otherwise the template is excluded and listed in the manifest.

Shipped grids (uniform-ratio chains, equal modal damping 2, 5, 10, 20 %):
`mdof2-*`: mass and stiffness ratios {0.25, 0.5, 1, 2} (64 templates);
`mdof3-*`: {0.5, 1, 2} (36 templates). Raw and b = 40 conditioned versions.
2DOF/3DOF scores are reported separately; a more flexible family is never chosen automatically
because its best-of-many R2 is larger.

## Shipped banks

| bank_id | templates | data_sha256 (prefix) |
|---|---|---|
| sdof-raw-v1 | 9 | 1ed8d8c614f12bd2 |
| sdof-ko20-v1 | 9 | 1753875c8ae9def3 |
| sdof-ko30-v1 | 9 | 75268d540f05a2d5 |
| sdof-ko40-v1 | 9 | 79169e46a7f50cc8 |
| sdof-ko50-v1 | 9 | e87a7215ee98ea4c |
| sdof-ko60-v1 | 9 | 11576173d88d3f48 |
| sdof-ko80-v1 | 9 | 7b8fcddad9df4906 |
| sdof-ko100-v1 | 9 | 33d63d83f6bd1f70 |
| mdof2-raw-v1 | 64 | ee08c080206da066 |
| mdof2-ko40-v1 | 64 | 27505796ec26e0e2 |
| mdof3-raw-v1 | 36 | 77bad0c3c1a5bd20 |
| mdof3-ko40-v1 | 36 | 2e05b07c946095b4 |

Regenerate: `uv run python scripts/build_banks.py` (about 80 s). Verify: `--check`. Generation is
byte-deterministic (fixed zip timestamps, sorted entries).

## Format and safe import

Manifest JSON (`schemas/template_bank.schema.json`) + NPZ of plain numeric arrays: `x` (129),
`log_templates` (K x 129), `param_values` (K x P), `anchor_ratio` (K), and for MDOF `modal_ratios`,
`modal_damping`. The loader rejects: schema violations, SHA-256 mismatch, non-zip data, archive entries
with paths or unexpected names, object/pickled arrays (`allow_pickle=False`), non-numeric dtypes,
oversized arrays (64 MiB total), wrong grid or shapes, non-finite values, templates not normalized at
q = 1 or not peaked there, and inadmissible modal data. Optional HMAC-SHA256 manifest signatures
(`sign_manifest` / `verify_signature`) can be required for administrator-activated banks. Additional
bank directories come from `HVSR_BANK_DIRS`. Old versions remain loadable for replay.
