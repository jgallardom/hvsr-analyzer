# Implementation status and work log

Specification: `docs/spec/HVSR_Analyzer_Cursor_Prompt_v2.md` (v2.0, SHA-256
`2568f155d696651db602c09f4e517191982615664a6a34189a11dde0c79d2d3e`).

Gate results are appended automatically to `artifacts/test-reports/gate-log.jsonl`; the acceptance
matrix is rendered to `docs/acceptance-matrix.md`.

Environment for all entries unless stated: Ubuntu Linux 6.17 (x86_64), 8 CPUs, 30 GiB RAM, Python
3.12.3, dependencies from `uv.lock` (numpy 2.5.4, scipy 1.18.1, obspy 1.5.1, arch 8.0.0, pydantic
2.14.1, fastapi 0.156.4, sqlalchemy 2.1.5; hvsrpy 2.1.0 as test oracle only).

---

## Stage 0 - Contracts, numerical conventions, fixtures

Goal: schemas, configuration model, units/timing rules, package layout, run contract, decision log,
fixtures, traceable acceptance IDs, executable gate.

IDs: AC-S0-01 ... AC-S0-09.

Delivered:
- uv workspace: `packages/hvsr_core` (standalone science + CLI) and `src/hvsr_web` (app).
- `hvsr_core.config` (pydantic, frozen, `extra=forbid`), scientific hash excluding report/display.
- JSON Schemas: analysis, manifest, template bank, config (generated).
- `docs/units-and-timing.md`, `docs/scientific-decisions.md`, `docs/configuration.md` (generated).
- Deterministic synthetic generator and four stored fixtures with manifest/checksums/license.
- `scripts/gate.py` with acceptance-ID tracing via JUnit properties.

Command: `uv run python scripts/gate.py --stage 0` -> exit 0, 10 tests passed, 9/9 IDs PASS.

## Stage 1 - Inputs, sensor checks, preservation

Goal: CSV/CSV.GZ (three layouts) and MiniSEED parsing, component mapping, gap/timing detection,
sensor/response status, StationXML, hashes, original preservation, verified canonical conversion,
canonical-only safety, photos, record identity.

IDs: AC-S1-01 ... AC-S1-21.

Delivered: `hvsr_core.io` (`csv_reader`, `mseed_reader`, `stationxml`, `preservation`, `limits`),
`hvsr_core.identity`, `hvsr_core.metadata.evaluate_instrument`, `hvsr_web.photos`,
`docs/supported-formats.md`.

Findings recorded:
- ObsPy 1.5.1 has no MiniSEED 3 reader -> MiniSEED 3 rejected explicitly.
- MiniSEED 2 (ObsPy writer) stores start times at microsecond precision -> exact nanosecond start is
  kept in the canonical sidecar (`miniseed_start_exact: false`).

Command: `uv run python scripts/gate.py --stage 1` -> exit 0, 40 tests passed (stages 0-1 +
regression), 30/30 IDs PASS, 1.8 s.

## Stage 2 - Standard HVSR scientific core

Goal: preprocessing, half-open integer windows, FFT, sparse Konno-Ohmachi, H/V with vertical guard,
valid-frequency mask, basic QC, Standard reference (+ optional FDWRA with basic-only audit), discovery
mean curve, peak families, source-line diagnostics, lower-band check, interpretation status.

IDs: AC-S2-01 ... AC-S2-19 (AC-S2-05 BLOCKED).

Delivered: `hvsr_core.preprocess`, `windows`, `spectra`, `qc.screen_windows`, `selection`, `curves`,
`peaks`, `core.run_core`; tests in `tests/stage_02/`.

Findings recorded:
- hvsrpy 2.1.0 agrees with our per-window and mean H/V to relative 1e-9 when fed identical windows
  and `fft_settings={"n": None}` (D-016). hvsrpy pads to >= 2^15 samples by default.
- Classic STA/LTA (STA 1 s) rejects most windows when horizontals are dominated by narrow-band
  low-frequency energy; documented as a limitation with a runtime warning (D-020).
- Recurrence alone does not discriminate noise families on flat records; plausibility relies on the
  mean-curve prominence rule (D-030). False-discovery rate to be measured in Stage 5.

Blocked:
- AC-S2-05 Geopsy comparison. Geopsy is not installed and no redistributable Geopsy output exists.
  Next action: install Geopsy (or obtain Geopsy outputs for `tests/fixtures/*`) and add
  `tests/stage_02/test_geopsy_reference.py`.

Command: `uv run python scripts/gate.py --stage 2` -> exit 0, 75 tests passed (stages 0-2 +
regression), 48 PASS, 1 BLOCKED (AC-S2-05).

## Stage 3 - Offline template generation and R-squared selector

Goal: SDOF bank (raw + Konno-Ohmachi conditioned), 129-point q grid, actual-peak normalization,
R2_log matching, eligibility statuses, greedy non-overlapping selection, bank manifests and safe
import, 2DOF/3DOF generator, critical no-alignment test.

IDs: AC-S3-01 ... AC-S3-20 (all PASS).

Delivered: `hvsr_core.templates`, `hvsr_core.mdof`, `hvsr_core.matching`, 12 shipped banks in
`packages/hvsr_core/src/hvsr_core/banks/`, `scripts/build_banks.py`, `docs/template-banks.md`,
benchmark `artifacts/benchmarks/template_conditioning.json`.

Findings recorded:
- Known-template interpolation tolerance derived from a sub-grid phase sweep (worst 1 - R2 = 5.6e-3
  for raw 1 % damping, <= 1.4e-4 otherwise); declared 1e-2 / 5e-4. An earlier 8-draw estimate
  (5e-3) was too optimistic and was replaced by the sweep before declaring.
- Raw templates against smoothed data bias damping labels upward (24/54 correct); conditioned b = 40
  bank 54/54 (D-044).
- Mutation tests confirm the CRITICAL test fails for peak-aligned or amplitude-normalized final curves.

Not yet done (later stages): CLI wrapper for bank build/validate (`hvsr banks ...`, Stage 9),
administrator upload/activation of banks (Stage 7).

Command: `uv run python scripts/gate.py --stage 3` -> exit 0, 131 tests (stages 0-3 + regression),
68 PASS, 1 BLOCKED (AC-S2-05).

## Stage 4 - Geometric mean and dependence-aware uncertainty

Goal: observed geometric mean, temporal block resampling of whole window spectra, peak-replicate
outcomes, interval status, one-band export contract.

IDs: AC-S4-01 ... AC-S4-12 (all PASS).

Delivered: `hvsr_core.uncertainty` (block planning, replicate counts, ratio-estimator replicate
means, percentile band with zero-width guard, block-length sensitivity, peak replicates with
multimodality detection, `selection_uncertainty`, `export_curve`); tests in `tests/stage_04/`.

Findings recorded (D-050 revisions 1 and 2, made before any validation run):
- With fixed 5 x Lw original-time blocks, a 20-30 min record has only 5-7 blocks, so the CI would be
  unestimable for nearly every SDOF selection. The base length is now 2 x Lw. The reason is block
  count (estimability), not band width.
- Politis-White on <= 50 windows, maximised over features, is dominated by estimator noise. In iid
  simulations it made the CI unestimable in 83 % of cases at n = 24 and 24 % at n = 50. It is now
  used only for regular sequences with >= 75 windows.
- The raw rule "lag-1 r > 0.3" doubled falsely in 31 % of iid cases with 11 block pairs. Doubling now
  also requires the one-sided Bonferroni bound; the measured null rate is <= 2.3 % (tested), and
  power for strong dependence (AR phi = 0.95) is >= 80 % (tested). Weak dependence can go undetected
  with few blocks; a low-power note is emitted and Stage 5 will measure the consequence.
- The band is labelled nominal and uncalibrated (`coverage_calibrated = false`) until Stage 5.

Not yet done (later stages): assembling these objects into `analysis.json` (Stage 6), plotting
(Stage 8), and coverage evaluation (Stage 5).

Command: `uv run python scripts/gate.py --stage 4` -> exit 0, 160 tests (stages 0-4 + regression),
80 PASS, 1 BLOCKED (AC-S2-05).

## Stage 5 - Offline selection-bias and coverage validation

Goal: reproducible Monte Carlo harness with disjoint development / locked test / truth seed sets,
predeclared hash-locked protocol, Standard and SDOF evaluated separately, honest report.

IDs: AC-S5-01 ... AC-S5-05 PASS; AC-S5-06 **UNVALIDATED** (scientific target not met);
AC-S5-07 BLOCKED (no licensed published layered benchmark).

Delivered: `hvsr_core.research.validation` (14 scenarios, per-replicate metrics, truth pooling),
`hvsr_core.research.validation_protocol` (protocol, lock check, parallel runner, binomial targets),
`scripts/validate_mc.py`, `docs/validation/protocol-v1.json` (commit 829d33e),
`docs/validation/protocol-v1.lock.json` (commit a3adb02, sha256 8768c5c5...),
`docs/validation/report-v1.md`, `artifacts/validation/{dev,test}-v1-*`.

Locked test set (400 replicates x 14 scenarios, 200 truth records per scenario, run once after the
lock): 23 of 28 predeclared targets passed. Failed or not evaluable:
- regime_switching: Standard 174/232 = 75.0 % and SDOF 188/256 = 73.4 % coverage of the policy mean,
  plus 42 % / 36 % no-result rate. Minute-scale H/V regime changes are not detected by the
  adjacent-block test with ~18 blocks, so the band is too narrow.
- flat_colored_episodes and colored_sdof (alpha = 1.5): default STA/LTA rejects nearly every window,
  so no CI is produced (NOT EVALUABLE). Milder colouring (alpha = 0.75) and episodes alone pass.
Other findings (reported, not targets):
- Fundamental chosen as the family with the best SDOF R2 would be wrong in 400/400 weak-fundamental
  replicates and 128/400 two-peak replicates; the predeclared lowest-plausible-family rule is correct
  in 399/400 and 400/400.
- SDOF selection raises R2 but adds a +3 to +4 % peak-frequency bias on non-SDOF shapes (machine tone,
  episodes, transients, regime switching), where Standard is within +/-1 %.
- Asymmetric broad peak: both methods are biased by about -6 % in fp, so the conditional peak interval
  covers the true resonance only 62 % (Standard) and 35 % (SDOF) of the time.
- CI coverage of the physical transfer-function amplitude is 31-91 %, as expected: the CI targets the
  smoothed policy mean, not |T(f)|.
- Null false-peak rate: 0/400 (white), 42/400 (alpha = 1.5 with episodes; within the 10 % target).

Consequence: the CI stays labelled nominal and uncalibrated (`coverage_calibrated = false`). The
uncertainty and QC methods are experimental; no validated 95 % claim is made.

Command: `uv run python scripts/gate.py --stage 5` -> exit 0, 85 PASS, 2 BLOCKED (AC-S2-05,
AC-S5-07), 1 UNVALIDATED (AC-S5-06).
