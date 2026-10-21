# HVSR Analyzer

Horizontal-to-vertical spectral ratio (HVSR) analysis of three-component ambient-vibration
recordings, as an offline command-line tool and a web service for sharing measurements.

For each recording it computes two curves, each the unweighted geometric mean of the original
per-window H/V spectra on a common frequency grid:
- **Standard**: all windows that pass quality control;
- **SDOF**: windows whose spectrum near a peak best matches precomputed single-degree-of-freedom
  templates, ranked by residual R-squared in log amplitude. Peak alignment is used only for this
  scoring; the curve averages the windows at their original frequencies.

Each curve has a bootstrap band labelled "Estimated 95% pointwise CI for the selected-window
mean". The band is nominal and selection-conditional: its coverage has **not** been validated (see
"Validation status"). SESAME criteria are reported, never used to change results.

The specification is `docs/spec/HVSR_Analyzer_Cursor_Prompt_v2.md`. Scientific conventions not
fixed by it are recorded in `docs/scientific-decisions.md`.

## Validation status

- Implemented and gate-tested: stages 0 to 9 of the specification (`docs/acceptance-matrix.md`).
- **UNVALIDATED**: the Monte Carlo coverage and false-peak targets (AC-S5-06). The locked test set
  failed 2 of 28 targets (regime-switching noise) and could not evaluate 3 (coloured noise
  rejected by quality control). See `docs/validation/report-v1.md`.
- **BLOCKED**:
  - comparison against Geopsy (AC-S2-05): Geopsy not available;
  - a published layered-site benchmark (AC-S5-07): no redistributable dataset;
  - the Docker image build (AC-S9-09): no Docker on the development machine.
- Not done: the CI workflow has not run on GitHub, and nothing has been deployed.

## Install

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync               # all dependency groups, from uv.lock
uv run hvsr --help
```

## Offline analysis (no server, database, or credentials)

```bash
uv run hvsr analyze recording.csv.gz --csv-layout elapsed_time --out results/ --plots
uv run hvsr replay results/analysis_*.json --recording recording.csv.gz --csv-layout elapsed_time
```

- `analyze` accepts CSV, CSV.GZ, or MiniSEED, and optionally StationXML (`--stationxml`) and
  sensor metadata (`--metadata`). It writes the analysis JSON, mean-spectrum and per-window CSV
  tables, SVG figures, and the robustness run B. Options: `--request request.json` (a full
  analysis request; `uv run hvsr schema config` prints its schema) and `--cache DIR`
  (reuse identical results). Input formats: `docs/supported-formats.md`.
- `replay` recomputes a published or offline analysis from the original recording and reports
  `reproduced` (exit 0) or the differences (exit 1). It works on JSON files downloaded from the
  web service; supply every method's analysis JSON of the run.

## Template banks

Eight SDOF banks and four 2DOF/3DOF banks ship with the package (`uv run hvsr banks list`). To use
a different bank:
1. Generate it on your own computer: `uv run hvsr banks build ...`.
2. Check it: `uv run hvsr banks validate BANK.json`.
3. Upload the two resulting data files (JSON manifest and NPZ arrays) as an administrator.
4. Activate that bank version.

Banks are data only, never code or pickles, and every version stays available for replay. Full
steps: `docs/template-banks.md`.

## Monte Carlo experiments

- `uv run hvsr mc --n 10 --out mc/` runs a development-set experiment. It is exploratory only.
- The predeclared, hash-locked validation protocol is run with `scripts/validate_mc.py`. Changing
  the method requires a new protocol version with fresh seeds; never rerun v1. Details are in
  `docs/validation/`.

## Web service

```bash
cp .env.example .env    # set HVSR_URL_SIGNING_KEY and HVSR_EMBEDDED_WORKER=true at least
set -a; . ./.env; set +a
uv run uvicorn hvsr_web.api:create_app --factory --port 8000
uv run python -m hvsr_web.admin issue-upload-token --email you@example.org
```

Open `http://127.0.0.1:8000`, upload a recording with the token, request an analysis, and open
the results. Every setting (limits, quotas, storage) is an `HVSR_*` environment variable listed in
`.env.example` and `docs/configuration.md`.

Operator commands (`python -m hvsr_web.admin ...`):
- `migrate`;
- `issue-admin-token`, `issue-upload-token`;
- `backup create|verify|restore`;
- `banks list|upload|activate|deactivate`;
- `sweep-staging`.

## Deployment

`render.yaml` deploys one Render web service with the worker embedded, a Render PostgreSQL
database, and an S3-compatible bucket (Cloudflare R2 by default). The guide is in
`docs/deployment.md`. Related documents:

| Topic | Document |
|---|---|
| Monthly costs for 100, 1,000, and 10,000 measurements | `docs/costs.md` |
| Measured time and memory | `docs/performance.md` |
| Limits and quotas, including hosted values | `docs/quotas-and-limits.md` |
| Backup and restore | `docs/backup-restore.md` |
| Security scans | `docs/security-scans.md` |
| Storage, jobs, and workers | `docs/storage-and-jobs.md` |
| Licensing and moderation | `docs/licensing-and-moderation.md` |

## Tests and acceptance gates

```bash
uv run python scripts/gate.py --all        # every stage + regression tests; renders the matrix
uv run python scripts/gate.py --stage 4    # stages 0..4 only
uv run ruff check . && uv run ruff format --check .
```

- The gate fails unless every acceptance ID has a passing test or a documented BLOCKED or
  UNVALIDATED status (`docs/acceptance/requirements.toml`).
- The PostgreSQL tests start a throwaway server (`pgserver`), or use `HVSR_TEST_POSTGRES_URL`.
- The smoke tests start real servers on free local ports.
- CI runs the same command (`.github/workflows/ci.yml`).

## Resuming development

1. Read "Next action" at the end of `docs/implementation-status.md`.
2. Run `uv run python scripts/gate.py --all` and work on the first acceptance ID that is not PASS.
   The rules for agents and humans are in `AGENTS.md`.

## License

BSD-3-Clause (`LICENSE`).
- `hvsrpy` (GPL-3.0) is used only as a numerical reference in tests. It is never imported by
  shipped code or installed in the image.
- Uploaded measurements carry their own licenses (default CC BY 4.0). See
  `docs/licensing-and-moderation.md` and `docs/citation.md`.
