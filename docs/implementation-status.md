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

## Stage 6 - SESAME report, replay, robustness comparison, quality dimensions, annotations

Goal: the official SESAME checks as a reporting-only diagnostic with documented boundary conventions;
exact-input replay; the predeclared 1.5 x Lw robustness comparison; separate quality dimensions; and
attributed human annotations.

IDs: AC-S6-01 ... AC-S6-12.

Delivered:
- `hvsr_core.sesame`: R1-R3 and C1-C6 with measured values, thresholds, comparison, status and reason.
  Existential and universal checks handle coverage, and all scalar comparisons go through one strict
  comparator. Source title/URL are persisted; the checksum is NOT verified (`sha256: null`, with a note).
- `docs/sesame-implementation.md`: conventions C-1 to C-5, labelled as implementation choices.
- `hvsr_core.analysis`:
  - `compute_science` and `science_docs` produce the per-method documents, which validate against
    `analysis.schema.json`.
  - `apply_report` is a pure reporting layer.
  - `AnalysisCache` holds immutable results in memory or as JSON (first result wins).
  - `run_analysis` runs A and B (both methods each) and produces the comparison record.
  - `verify_replay` bypasses the cache and recomputes. Science is compared within the tolerance
    policy; provenance is compared exactly and reported separately.
- `hvsr_core.robustness`: criteria predeclared in D-060, before any comparison result was examined.
- `hvsr_core.annotations`: an append-only, frozen, `extra=forbid` log; `merge_for_display` keeps
  computed documents unchanged and in a separate key.
- Decisions D-060 (robustness criteria) and D-061 (quality dimensions, no summary score).

Observed robustness outcomes (fixed criteria, 1800 s synthetics at 50 Hz):
- Clean 2 Hz SDOF, seeds 1-5: all consistent for both methods. The largest |ln fp ratio| was 0.011,
  and the largest median curve difference was 0.019.
- Broad 0.5-decade plateau peak at 3 Hz, seeds 1-5: 3 of 5 flagged `sensitive` by Standard
  (|ln fp ratio| 0.184 and 0.080, and one changed plausible-family set), 2 of 5 consistent. SDOF had
  no matched resolved family on these records (`not_evaluable`).
- Two-peak record: both families are compared separately; both are consistent.
- Run B with too few windows (20 s / 30 s on 110 s), or with no fitting window: `not_evaluable`,
  with the planned B length reported and no alternative B tried.

Boundary tests:
- Every SESAME threshold is tested just below, at, and above, with float64-exact constructions where
  the boundary is representable.
- Where it is not (exp(c) never equals exactly 3.0 or some theta values), the adjacent reachable
  values are tested, and equality is tested on the shared comparator.

Known limits:
- The SESAME PDF checksum is unverified.
- The robustness tolerances are research criteria checked only on synthetics.
- No summary score is computed.

Command: `uv run python scripts/gate.py --stage 6` -> exit 0, 213 tests passed, 97 PASS, 2 BLOCKED
(AC-S2-05, AC-S5-07), 1 UNVALIDATED (AC-S5-06). `ruff check .` and `ruff format --check .` are clean.

---

## Stage 7 - Durable storage, quotas, licensing, and job execution

Goal: object storage, database migrations, artifact commits, stable URLs, the contributor quota
ledger, CC BY consent, moderation, recoverable jobs, and one demonstrated backup restore.

IDs: AC-S7-01 ... AC-S7-19.

Delivered (`src/hvsr_web/`; design in `docs/storage-and-jobs.md`, `docs/quotas-and-limits.md`,
`docs/licensing-and-moderation.md`, `docs/backup-restore.md`):
- `settings.py`: every secret and limit is an `HVSR_*` environment variable.
- `db.py` and `migrations/` (Alembic revision `0001`, 13 tables). SQLite serializes writers with
  `BEGIN IMMEDIATE`. Downgrades are refused unless explicitly authorized.
- `storage.py`: local and S3 adapters. Committed keys are content-addressed and immutable; only
  `staging/` is mutable. Each request to a stable link issues a fresh HMAC- or SigV4-signed URL.
- `submissions.py`: validate, reserve, stage, write and verify, then one publishing transaction.
  The staging sweeper also releases stale reservations left by crashed submissions.
- `quota.py`, `auth.py`, `records.py`, `licensing.py`, `moderation.py`, `manifests.py`.
- `jobs.py` and `analysis_jobs.py`:
  - durable claims with leases and fencing tokens;
  - a compute-budget estimate checked at request time and again by the worker;
  - science documents cached immutably in the object store;
  - one fenced publication transaction for runs, artifacts, compute usage, and job completion.
- `backup.py`: JSON Lines per table plus objects with a SHA-256 manifest. Restore goes only into an
  empty, migrated database, and the result is verified.
- `api.py`: a minimal FastAPI app covering health, limits, manifests, artifacts, blobs, comparisons,
  submissions, analyses, jobs, reports, and admin remove/restore. The web pages are Stage 8.
- `hvsr_core.io.preservation.recording_from_canonical`. Its `recording_hash` equals the original
  parse for the elapsed-time, UTC-timestamp, and samples-only CSV fixtures.

Defects found and fixed during this stage:
- Stage 6 cache key: the science key omitted the record identity block that the documents embed.
  The same bytes submitted as two measurements, or a metadata correction, would have been served
  documents with the wrong record code or stale coordinates. The key now includes the block, and a
  regression test was added to AC-S6-12.
- Default CSV submissions stored a FLOAT64 canonical MiniSEED (737 KB for a 604 KB CSV.GZ) next
  to the original. It is now `on_demand`; the fixture's stored bytes are now 606,584 (the original
  plus small metadata objects), without the 737 KB canonical copy.
- MiniSEED was charged by file size, so Steim2 compression was a quota bypass (196,608 bytes on
  disk vs 360,000 decoded). It is now charged at the larger of the two.
- A crashed submission's `reserved` ledger entry held quota indefinitely. The sweeper now releases
  it after the staging TTL, but never for an accepted submission.
- Reader limit errors surfaced as generic `InputError` (HTTP 422). All `*_limit` codes now surface
  as `LimitExceeded` (HTTP 413).
- Plain-CSV originals are stored as gzip. Redirecting to the blob would have served gzip bytes
  under a `.csv` name, so the artifact endpoint now decompresses and verifies the exact original.
- Manifest run order depended on database insertion order, so it changed after a restore (found
  by the backup test failing intermittently in the full suite). Runs are now ordered by
  (created, role, run ID); the backup test passed 10 of 10 repeats.
- `from conftest import ...` in Stage 6 resolved to Stage 7's `conftest.py` once a second stage
  folder had one. Helpers now live in uniquely named `stageN_support.py` modules (noted in
  `AGENTS.md`).

Observed on fixtures:
- A 600 s, 50 Hz CSV.GZ analysis job takes about 0.3 s wall time. It produces run A and run B, four
  analysis JSONs, and a robustness comparison.
- The cost estimate for that record is 111 and 106 candidate windows. A SESAME-off request
  reuses the run and adds report-variant artifacts only.
- Concurrency: 16 concurrent 1000-byte reservations against a 10,000-byte budget gave exactly 10
  accepted. Three concurrent submissions against 2.5 x the logical size gave 2 accepted with codes
  `X` and `X-02`.

Known limits:
- Per-job CPU and wall-time ceilings are configured but not yet enforced by process isolation
  (Stage 9).
- `on_demand` artifacts (normalized CSV, canonical recording in default mode, reconstructed CSV)
  return HTTP 409 until Stage 8 adds generation.
- Content-addressed objects written by a submission that failed after step 4 stay unreferenced;
  they are not garbage-collected automatically.
- S3 immutability relies on conditional writes (`If-None-Match`); providers that ignore them lose
  the overwrite protection for `analyses/science/` keys.
- SQLite is single-host; PostgreSQL is supported by the models and migrations but was not
  exercised in this environment.
- The S3 adapter is tested against moto, not a live provider. No deployment or remote push has
  been done.

Command: `uv run python scripts/gate.py --stage 7` -> exit 0, 240 tests passed, 116 PASS, 2 BLOCKED
(AC-S2-05, AC-S5-07), 1 UNVALIDATED (AC-S5-06). `ruff check .` and `ruff format --check .` are clean.

---

## Stage 8 - Web interface and independent exports

Goal:
- Three pages (upload, measurement, results), plus history, configuration, photos, public records,
  and search.
- The one-band plot, sensor and quality warnings, method and family toggles.
- Independent downloads: original, canonical, normalized waveform, numerical tables, and figures.
- Manifests in which every link resolves or states why it does not.

IDs: AC-S8-01 ... AC-S8-13.

Delivered:
- Pages (`src/hvsr_web/pages.py`, `templates/`, `static/`): server-rendered HTML with a strict
  Content Security Policy. JavaScript is used only for the upload form, job polling, and on-request
  downloads.
- Results title is `RECORD_CODE (latitude, longitude)` on the page, in `<title>`, and on the
  figure.
- The figure is served as a `<picture>`: a narrow-screen SVG at <= 900 px and a desktop SVG above
  that.
- `hvsr_core/plots.py`: static Matplotlib figures (PNG, SVG, narrow-screen SVG) from a stored recipe
  (schema `plot_recipe`).
  - One band, labelled exactly "Estimated 95% pointwise CI for the selected-window mean", and only
    when the CI is estimable; otherwise the reason is printed.
  - Provenance footer; no SESAME text.
  - `series_sha256` of the plotted arrays.
- `hvsr_core/exports.py`: mean-spectrum, per-window (Standard and SDOF), per-window spectra, and
  normalized-waveform CSV tables, plus the data dictionary (schema `data_dictionary`;
  `docs/data-dictionary.md`).
- `hvsr_core/strings.py` and `locales/en.json`: every interface string. See decision D-081 for the
  `translate="no"` boundary.
- `publication.py` (run artifacts), `ondemand.py` (on-request generation), `runs.py` (cosmetic
  re-render), `worker.py` (separate or embedded worker). See `docs/storage-and-jobs.md`.
- Documentation:
  - `docs/citation.md`;
  - `docs/examples/`: real example exports, written by `scripts/export_examples.py`;
  - `docs/data-dictionary.md`: generated by `scripts/gen_docs.py`;
  - decisions D-080 to D-086.

Manual browser check (Chromium in Cursor; local server at 127.0.0.1:8791 with a throwaway
database). Screenshots are in `docs/evidence/stage8/`.
- `run-page-340px-compact.png`: 340 px viewport at device pixel ratio 2. The narrow-screen SVG is
  served and spans the full 340 px with no horizontal overflow (document width 340). Plain-number
  log ticks.
- `run-page-desktop.png`, `run-page-desktop-overlay.png`: 1280 px viewport. The desktop SVG is drawn
  900 px wide with the band and its exact label. The overlay view adds 28 selected windows and the
  Standard mean.

These screenshots were retaken after the tick-label fix below. The first set showed the superscript
labels.

Defects found and fixed during this stage:
- Log-axis tick labels used math-text superscripts (`10^-1`) rendered at 6.65 pt and 5.95 pt, below
  the readability criterion. Ticks are now plain numbers and math text is off (D-080).
- The run page showed no attribution or license. It now has an `#attribution` line linking to the
  measurement's license section.
- Published analysis JSON had `licensing: null`. It now embeds the fixed licensing fields (D-084).
- Figure size was unbounded: `DisplayOptions` allows 30 x 30 in at 600 dpi, an 18,000 x 18,000 px
  PNG of about 1.3 GB in memory. That size was reachable through an analysis request or a re-render,
  and custom variants per run were unlimited. Added `HVSR_MAX_PLOT_PIXELS` (checked in the compute
  budget and on re-render) and `HVSR_MAX_PLOT_VARIANTS_PER_RUN` for custom-size variants
  (`docs/quotas-and-limits.md`).
- This stage added science supplement documents (`analyses/science/{key}.supplement.json`, the
  per-window tables). Purge did not know about them, so it now deletes them with the science
  documents.

Changes to earlier-stage tests (behavior added by this stage, no assertion weakened):
- `tests/stage_07/test_jobs.py`:
  - Run-artifact counts now include the tables and figures this stage publishes (still exact
    counts; analysis-run kinds are now checked per kind).
  - Two tests decoded every run artifact as JSON. They now decode JSON artifacts only and also
    verify the SHA-256 of every stored run artifact. This failure was found by the first full gate
    run of this stage.
- `tests/stage_07/test_storage.py`: the on-demand normalized waveform label is now "Normalized
  waveform CSV.GZ", because it is gzip.

Observed:
- Stage 8 suite: 39 tests in 18 s.
- Example record (1800 s at 50 Hz): the analysis took under 3 s end to end through the API.
- Per-window spectra for that record: 145,314 rows, 1.8 MB compressed.

Known limits:
- The figure readability check computes rendered text size from the stylesheet rules and the served
  SVGs. A browser was used only for the three screenshots above, not for every width.
- Only an English catalog exists. Translation needs a second locale file. API `detail` strings stay
  English diagnostics.
- The custom-size variant cap is checked before rendering, not atomically.
- Replay compares science documents. A replay starting from a published analysis JSON must drop the
  publication-only fields first: `licensing`, `sesame` sections, `robustness_comparison_url`, and
  `windows.candidates_url`. That belongs with the Stage 9 CLI.
- External links (CC BY, SESAME) are not fetched by the tests (D-085).
- Per-job CPU and wall-time ceilings are still not enforced by process isolation (Stage 9).
- No deployment or remote push has been done.

Command: `uv run python scripts/gate.py --stage 8` -> exit 0 in 64 s, 279 tests passed, 129 PASS,
2 BLOCKED (AC-S2-05, AC-S5-07), 1 UNVALIDATED (AC-S5-06). `ruff check .` and `ruff format --check .`
are clean. The Stage 5 fingerprinted files are unchanged (`git diff` is empty for all 15).

---

## Stage 9 - Efficiency, security, and deployment handoff

Goal:
- Profile cached and uncached processing and both parameter runs on declared hardware.
- Bound concurrency, numerical threads, uploads, CPU and wall time, and memory per job.
- Reject malformed inputs and malformed banks; administrator bank upload and activation.
- Dependency and static security scans.
- The `hvsr` CLI for offline work.
- Dockerfile, Render blueprint, and CI calling the same gate.
- Cost assumptions, backup and restore procedure, and startup smoke tests.

IDs: AC-S9-01 ... AC-S9-14. AC-S9-13 (offline CLI) and AC-S9-14 (PostgreSQL) were added during this
stage: the specification's deliverables include the CLI and a hosted PostgreSQL deployment, and
neither had an acceptance test.

Delivered:
- Job isolation (`src/hvsr_web/isolation.py`, D-090):
  - each job runs in a spawned child process with CPU, memory, and wall-time limits, and pinned
    numerical threads;
  - exhausting a limit is a permanent failure; other crashes are retried durably; the lease is
    renewed while the job runs.
- Uploads are parsed in the thread pool, at most `HVSR_MAX_CONCURRENT_UPLOADS` at a time; a further
  upload gets 503 busy (D-091).
- PostgreSQL:
  - advisory locks serialize migrations, job claims, and quota reservations (D-092);
  - hosted `postgres://` URLs use the installed psycopg 3 driver;
  - tests run on a throwaway PostgreSQL 16 server (`pgserver`), or on `HVSR_TEST_POSTGRES_URL`.
- Template banks (`banks.py`, D-096):
  - administrator-only upload, activation, and deactivation, over the API and `hvsr_web.admin`;
  - data-only validation; immutable versions;
  - migration `0002_template_banks` (additive);
  - details in `docs/template-banks.md`.
- `hvsr` CLI (`hvsr_core/cli.py`, `replay.py`): `analyze`, `replay`, `banks build|validate|list`,
  `mc`, `docs`, and `schema`. It runs without the web stack.
- Operator CLI (`hvsr_web/admin.py`): `migrate`, tokens, `backup create|verify|restore`, banks,
  `sweep-staging`.
- Malformed input:
  - fuzzed readers raise only input errors;
  - a StationXML DOCTYPE is rejected in any encoding;
  - CSV is streamed with packed samples (D-094);
  - malformed uploads and requests get clear 4xx responses and store nothing.
- Performance (`scripts/profile_runs.py`, `docs/performance.md`): six cases from 15 min to 24 h,
  measured both at the defaults and at the Render blueprint's limits.
- Security (`scripts/security_scan.py`, `docs/security-scans.md`): pip-audit against PyPI and OSV,
  and bandit.
- Deployment:
  - `Dockerfile`, `scripts/docker-entrypoint.sh`, `.dockerignore`, `render.yaml`, `.env.example`;
  - `.github/workflows/ci.yml`;
  - `scripts/smoke.py` (local, clean install, Docker, URL);
  - `docs/deployment.md`.
- Costs (`scripts/cost_model.py`, `docs/costs.md`): 100, 1,000, and 10,000 measurements per month.
  Prices were copied from the official Render, Cloudflare R2, Backblaze B2, and Hetzner pages on
  2026-10-21 (`docs/evidence/stage9/prices.json`).
- Documentation:
  - new: `README.md` (rewritten), `docs/deployment.md`, `docs/costs.md`, `docs/performance.md`,
    `docs/security-scans.md`;
  - updated: `docs/backup-restore.md`, `docs/quotas-and-limits.md`, `docs/storage-and-jobs.md`,
    `docs/template-banks.md`, and `AGENTS.md` (including the validation lock);
  - decisions D-090 to D-099.

Measured (development machine, one numerical thread; `docs/performance.md`):
- Cold job for both runs:

  | Case | Wall time | Peak memory |
  |---|---|---|
  | 1 h at 100 Hz | 4.3 s | 283 MB |
  | 2 h at 200 Hz | 8.7 s | 551 MB |
  | 24 h at 50 Hz | 31.5 s | 1,583 MB |

- Warm requests reuse the stored science and run no analysis (AC-S9-05).
- At the blueprint limits, the worst admitted case (8 h at 50 Hz, 5,751 candidate windows) peaks
  at 664 MiB per job.
- Security scans on 2026-10-21:
  - pip-audit: no known vulnerabilities in 128 audited packages, from either source;
  - bandit: 0 high, 0 medium, and 23 low findings, all justified in `docs/security-scans.md`.

Defects found and fixed during this stage:
- **Event-loop stall.** Async upload handlers parsed synchronously, so one large upload blocked
  health checks and every other request (D-091).
- **PostgreSQL races, hidden by SQLite:**
  - concurrent startup migrations;
  - the job concurrency ceiling;
  - concurrent retries of one submission key (D-092).
- **Backup checksums depended on the database's text collation.** A PostgreSQL backup did not
  verify on SQLite. Rows are now sorted by the application.
- **Hosted `postgres://` URLs** selected psycopg2, which is not installed.
- **StationXML DTD guard bypass** with UTF-16 or a long prolog: medium severity, entity-expansion
  exhaustion.
- **CSV parsing memory.** About 760 MB for a 2 h, 200 Hz upload; now about 290 MB.
- **HTTP 500 responses** for an over-long CSV field and for non-UTF-8 JSON bodies; both now 422.
- **A method with no window selection broke publication and the results page.** The run now
  publishes and the page states why there is no curve (D-095).
- **Process management:**
  - the standalone worker ignored SIGTERM; it now finishes the current job and exits;
  - uvicorn honours `WEB_CONCURRENCY`, which would start several workers; the entry point pins one.
- **Provenance recorded no commit in containers** (no git); `HVSR_GIT_COMMIT` is now set from the
  build or Render.
- **Empty `HVSR_*` variables from a copied `.env` were taken literally**, for example an empty S3
  endpoint. They now count as unset (D-098).
- **`hvsr schema` with an unknown name printed a traceback**; it now gives a usage error with
  exit code 2.
- **bandit B314** in `scripts/gate.py`: the JUnit XML is now parsed with defusedxml.
- **Flaky AC-S9-01 reproducibility test.** It compared analysis-document byte counts as if they
  were deterministic, but each document records the checkout's `git_dirty` flag. The test failed
  (4 bytes per run) when the gate ran on a modified tree against a report made from a clean one.
  It now drops that byte count only when the two checkout states differ. The profiler also
  records the checkout state before profiling, the same state the analyses see.

Changes to earlier-stage code and tests (no assertion weakened, no fingerprinted file touched):
- `tests/stage_07/test_backup.py` compared the backup's schema revision with the literal `"0001"`.
  It now compares with the Alembic head, because this stage adds migration `0002`.
- `hvsr_core/plots.py`: `main_selection` raises a clear error for a method with no selection.
  `io/csv_reader.py` and `io/preservation.py` stream lines. Old and new readers agree on values and
  errors.
- `scripts/gate.py`:
  - a BLOCKED requirement whose tagged tests ran and passed is reported as PASS for that run
    (D-099);
  - the JUnit XML is parsed with defusedxml.

Command: `uv run python scripts/gate.py --all` (stage 9 includes every earlier stage), on a clean
checkout of commit `8ec3f80`. It exited 0 in 141 s:
- 329 tests passed, 1 skipped (the Docker smoke test);
- 142 PASS, 3 BLOCKED (AC-S2-05, AC-S5-07, AC-S9-09), 1 UNVALIDATED (AC-S5-06).

`ruff check .` and `ruff format --check .` are clean. The 15 Stage 5 fingerprinted files and the
shipped banks are unchanged (`git diff` is empty). The gate was run outside the agent sandbox,
because it needs subprocess resource limits, local servers, PostgreSQL, and `uv sync`.

Known limits and what was not verified:
- **Docker image never built** (AC-S9-09 BLOCKED). The Docker smoke test runs in CI, but CI has not
  run: the repository has not been pushed to GitHub. No credentials were used, and nothing was
  pushed or deployed. *Superseded: the repository was pushed and the image built and passed in CI;
  see "After Stage 9 - first GitHub Actions runs".*
- **Render blueprint** validated against Render's published JSON Schema (vendored), not by Render.
- **S3 adapter** tested against moto only. R2, B2, and Hetzner behaviour (conditional writes,
  SHA-256 checksums) is untested.
- **Hosted CPU speed** not measured. The cost model's capacity check assumes hosted CPUs are 2x
  slower.
- **Cost model:**
  - its workload (page views, downloads, object writes) is an assumption;
  - prices were copied by hand and will change;
  - the 10,000-per-month database plan was not load-tested.
- **Upload slots** are per web process. A separate worker or several web processes multiply them.
- **Idempotency keys** do not compare file contents (D-093).
- **obspy warning.** For some corrupted MiniSEED input, obspy's libmseed log callback raises an
  ignored `UnicodeDecodeError` (a pytest warning). Our readers still raise only input errors.

---

## After Stage 9 - first GitHub Actions runs

Goal: push to [jgallardom/hvsr-analyzer](https://github.com/jgallardom/hvsr-analyzer) (public)
and make the CI gate pass there. IDs involved: AC-S5-02, AC-S3-12, AC-S8-10, AC-S9-01, AC-S9-06,
AC-S9-09.

First run, [39622608537](https://github.com/jgallardom/hvsr-analyzer/actions/runs/39622608537),
commit `745c84c`:
- The security job passed: pip-audit (PyPI and OSV) and bandit.
- The gate failed one test out of 330: AC-S5-02's check that the validation protocol was committed
  before the locked test set ran. It runs `git merge-base --is-ancestor`, and `actions/checkout`
  clones at depth 1, so the commit that ran the test set was missing (git exit 128). A local shallow
  clone reproduced it.
- The Docker smoke test ran for the first time and passed (AC-S9-09).
- Fix (`a67a0b5`): the gate job checks out the full history (`fetch-depth: 0`), and the CI structure
  test requires it.

Second run, [39626471556](https://github.com/jgallardom/hvsr-analyzer/actions/runs/39626471556),
commit `a67a0b5`:
- AC-S5-02 passed, and the Docker smoke test passed again.
- Three byte-exact tests failed on last-bit differences: AC-S3-12 (bank regeneration), AC-S8-10
  (example CSV), and AC-S9-01 (analysis document sizes). Every count, selection, and status matched.
  The same code had passed them on the first run.
- A temporary probe workflow (24 jobs, two runs, branch deleted afterwards) traced this to the
  runner's CPU. AVX-512 runners use different NumPy and OpenBLAS code, and AVX2 runners reproduce
  the references exactly (D-100, `docs/evidence/stage9/numeric-path-probe.json`).
- Fix:
  - `tests/conftest.py` pins NumPy to the AVX2 target and OpenBLAS to its Haswell kernels where the
    CPU supports them. All 16 probe jobs passed the three tests with it, including 7 on AVX-512.
  - CI prints each runner's CPU and native code path.
  - `scripts/numeric_path_probe.py` repeats the measurement.
  - No assertion, expected value, or tolerance changed, and no fingerprinted file was touched.

AC-S9-09 keeps its BLOCKED status, now with the CI evidence in its reason. Removing it, as its old
note said, would turn local gate runs without Docker into failures, because a skipped test does not
count as a pass.

Command: `uv run python scripts/gate.py --all`, on the working tree with these changes before
committing. It exited 0 in 149 s:
- 329 tests passed, 1 skipped (Docker);
- 142 PASS, 3 BLOCKED (AC-S2-05, AC-S5-07, AC-S9-09), 1 UNVALIDATED (AC-S5-06).

`ruff check .` and `ruff format --check .` are clean.

---

## Next action

All nine stages are implemented and the gate passes. What remains needs credentials, external
software, or new science:

1. **Keep CI green.** If a byte-exact test fails on one runner and passes on another, compare the
   CPU and code path printed by the "Record the runner's CPU" step with D-100 before changing
   anything.
2. **Deploy** (needs Render and bucket accounts). Follow `docs/deployment.md`, then:
   - run `scripts/smoke.py url <site> --blueprint-limits`;
   - check one real submission end to end;
   - rerun `scripts/profile_runs.py` on the hosted instance.
3. **Recheck prices** in `docs/evidence/stage9/prices.json` before deploying, then rerun
   `scripts/cost_model.py`.
4. **Scientific validation (AC-S5-06).** A new protocol version (v2) with fresh locked seeds, after:
   - a dependence-detection method with power for minute-scale regime changes;
   - a QC anti-trigger that works for low-frequency-dominated records.

   Do not rerun the v1 test seeds.
5. **External references:**
   - Geopsy outputs for the synthetic fixtures (AC-S2-05);
   - a licensed layered-site benchmark (AC-S5-07).
