# Performance report

Generated 2026-10-21T04:17:44Z by `scripts/profile_runs.py` (software 0.1.0, commit `7109358f15b9a8ee21c26f0cc71919a8d4be0201` with uncommitted changes). Regenerate with `uv run python scripts/profile_runs.py`.

## Declared hardware

- CPU: Intel(R) Core(TM) Ultra 7 258V (8 logical CPUs); memory 31657 MB
- OS: Linux-6.17.0-1033-oem-x86_64-with-glibc2.39; Python 3.12.3; NumPy 2.5.4; BLAS openblas 0.3.31.dev, openblas 0.3.34.106.0
- One numerical thread per process; local filesystem object store and SQLite in a temporary directory.
- These are measurements on this development machine, not a hosted service. Hosted CPUs (for
  example Render instances) differ; rerun this script there before relying on the numbers.

## Cases

Deterministic synthetic recordings (seeded), submitted as gzip CSV through the real submission
path. Default analysis configuration: run A (50 s windows) and robustness run B (75 s windows),
SDOF matching, block bootstrap. The warm request changes only a report option, so it must reuse
the stored science. `large_wide` is the `large` recording written with 12 instead of 6 decimals,
to measure how upload memory follows the CSV text size.

| Case | Duration | Rate | Samples/channel | Upload gzip / CSV text | Candidates A / B | Template comparisons A / B | Bootstrap replicates A / B |
|---|---|---|---|---|---|---|---|
| small | 15 min | 100 Hz | 90,000 | 2 MB / 4 MB | 171 / 166 | 1,485 / 1,440 | 0 / 0 |
| medium | 60 min | 100 Hz | 360,000 | 7 MB / 15 MB | 711 / 706 | 6,273 / 6,219 | 12,000 / 12,000 |
| large | 120 min | 200 Hz | 1,440,000 | 27 MB / 59 MB | 1431 / 1426 | 12,501 / 12,402 | 12,000 / 12,000 |
| large_wide | 120 min | 200 Hz | 1,440,000 | 40 MB / 84 MB | 1431 / 1426 | 12,501 / 12,402 | 12,000 / 12,000 |
| eight_hours | 480 min | 50 Hz | 1,440,000 | 28 MB / 61 MB | 5751 / 5746 | 48,015 / 46,521 | 12,000 / 12,000 |
| long | 1440 min | 50 Hz | 4,320,000 | 83 MB / 184 MB | 17271 / 17266 | 146,790 / 143,757 | 12,000 / 12,000 |

Bootstrap replicates count every replicate set drawn (base block length plus each predeclared
sensitivity length that has enough informative blocks). Zero means no curve CI was estimable;
the CI status of each published selection is:

- small run A: sdof: ci_not_estimable; standard: ci_not_estimable
- small run B: sdof: ci_not_estimable; standard: ci_not_estimable
- medium run A: sdof: ci_estimated; standard: ci_estimated
- medium run B: sdof: ci_estimated; standard: ci_estimated
- large run A: sdof: ci_estimated; standard: ci_estimated
- large run B: sdof: ci_estimated; standard: ci_estimated
- large_wide run A: sdof: ci_estimated; standard: ci_estimated
- large_wide run B: sdof: ci_estimated; standard: ci_estimated
- eight_hours run A: sdof: ci_estimated; standard: ci_estimated
- eight_hours run B: sdof: ci_estimated; standard: ci_estimated
- long run A: sdof: ci_estimated; standard: ci_estimated
- long run B: sdof: ci_estimated; standard: ci_estimated

## Time and memory

Peak memory is the resident-set peak of the process doing the phase, including the import
baseline of 170 MB. Job wall time includes starting the isolated
job process.

| Case | Submit wall / CPU | Submit peak | Cold job wall | Cold CPU | Cold peak | Warm job wall | Warm CPU | Warm peak | Warm reused science |
|---|---|---|---|---|---|---|---|---|---|
| small | 0.2 s / 0.2 s | 115 MB | 2.3 s | 2.1 s | 211 MB | 2.0 s | 1.7 s | 206 MB | yes |
| medium | 0.7 s / 0.6 s | 144 MB | 4.3 s | 4.1 s | 283 MB | 2.5 s | 2.3 s | 216 MB | yes |
| large | 2.6 s / 2.6 s | 291 MB | 8.8 s | 8.6 s | 551 MB | 4.4 s | 4.2 s | 336 MB | yes |
| large_wide | 5.5 s / 5.5 s | 353 MB | 11.4 s | 11.1 s | 551 MB | 7.0 s | 6.8 s | 435 MB | yes |
| eight_hours | 2.7 s / 2.6 s | 297 MB | 11.0 s | 10.7 s | 664 MB | 4.6 s | 4.4 s | 342 MB | yes |
| long | 12.4 s / 12.4 s | 634 MB | 32.9 s | 32.6 s | 1616 MB | 12.2 s | 11.9 s | 679 MB | yes |

## Stored bytes

| Case | Original stored | Run A artifacts | Run B artifacts | Per-window spectra | Database growth |
|---|---|---|---|---|---|
| small | 2 MB | 24 files, 1 MB | 22 files, 1 MB | on request | 116 KB |
| medium | 7 MB | 24 files, 3 MB | 22 files, 3 MB | on request | 116 KB |
| large | 27 MB | 24 files, 4 MB | 22 files, 4 MB | on request | 116 KB |
| large_wide | 40 MB | 24 files, 4 MB | 22 files, 4 MB | on request | 116 KB |
| eight_hours | 28 MB | 24 files, 11 MB | 22 files, 11 MB | on request | 116 KB |
| long | 83 MB | 24 files, 30 MB | 22 files, 30 MB | on request | 116 KB |

Database growth is the SQLite file growth for one measurement and its analysis (rows, JSON
summaries, ledger entries); object bytes live in the object store.

## Budgets against measurements

- Largest measured cold job: long, 32.6 s CPU, 32.9 s wall, 1616 MB peak.
- `HVSR_JOB_CPU_TIME_S` = 900 s and `HVSR_JOB_WALL_TIME_S` = 900 s: 28x the largest measured CPU time here,
  which leaves room for slower hosted CPUs. A job that exceeds either limit is stopped and recorded
  as a permanent failure (no retry).
- `HVSR_JOB_MEMORY_BYTES` = 2048 MB above the job-process baseline (170 MB); the largest measured job peak was
  1616 MB including the baseline. A worker instance needs at least that much memory per concurrent job.
- `HVSR_MAX_CANDIDATE_WINDOWS` = 20,000: the long case (24 h) produced 17,271 candidate windows with the default 50 s windows,
  so the cap admits roughly 28 h with default windows; longer requests are rejected by the cost
  estimate before any computation, even though `HVSR_MAX_DURATION_S` is larger.
- Uploads are read and parsed in the web process, at most `HVSR_MAX_CONCURRENT_UPLOADS` = 1 at a time
  (others wait up to `HVSR_UPLOAD_WAIT_S` = 30 s, then receive 503 busy). The largest submission peak was 634 MB
  (long, 4,320,000 samples per channel as CSV). Size the web instance for that many
  of the largest uploads you accept, or lower `HVSR_MAX_REQUEST_BYTES` and `HVSR_MAX_SAMPLES_PER_CHANNEL`.
- `HVSR_MAX_TEMPLATE_COMPARISONS` = 200,000,000 per family: the shipped SDOF banks hold 9 templates, so comparisons stay far below the cap; it matters for
  large uploaded banks.
- No speed claim is made beyond these measurements.
