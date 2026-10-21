# AGENTS.md - working protocol for coding agents and humans

This repository implements the specification `docs/spec/HVSR_Analyzer_Cursor_Prompt_v2.md`
(version 2.0, 2026-10-20). That document is authoritative. When it conflicts with an older
prompt, code comment, or habit, the specification wins.

## Where things are

| Path | Purpose |
| --- | --- |
| `packages/hvsr_core/` | Standalone scientific package + `hvsr` CLI. Must never import HTTP, DB, cloud, or credentials. |
| `src/hvsr_web/` | FastAPI application: uploads, storage adapters, quotas, jobs, public views. |
| `docs/acceptance/requirements.toml` | Single source of truth for acceptance IDs (stage, requirement, spec section, status override). |
| `docs/acceptance-matrix.md` | Rendered matrix with last gate results. Regenerate with `scripts/gate.py --render`. |
| `docs/implementation-status.md` | Stage-by-stage work log: goal, IDs, commands, results, next action. |
| `docs/scientific-decisions.md` | Versioned decision log. Every convention not fixed by the spec is recorded here. |
| `artifacts/test-reports/` | JUnit XML and gate summaries written by `scripts/gate.py`. |
| `tests/stage_NN/` | Acceptance tests for stage NN. `tests/regression/` holds cross-stage regression tests. |

## Mandatory protocol (spec section 15.1)

1. Before working on a stage, record its goal and acceptance IDs in `docs/implementation-status.md`.
2. Implement the smallest working vertical slice. No mock scientific values, no fabricated spectra.
3. Run `uv run python scripts/gate.py --stage N`. It runs that stage plus every earlier stage and
   the regression suite, writes JUnit XML, and fails (nonzero exit) if any acceptance ID lacks a
   passing test or a documented BLOCKED/UNVALIDATED state.
4. Record the exact command, environment, exit status, and elapsed time in the status log.
5. Fix failures before moving on. Never weaken assertions, change expected values, loosen
   scientific tolerances, or skip scenarios just to pass. Justified changes go in
   `docs/scientific-decisions.md` with evidence.
6. Commit the passing stage. Update "Next action" in the status log.
7. Missing credentials, external fixtures, or calibration => mark the affected acceptance ID
   `BLOCKED` or `UNVALIDATED` in `requirements.toml` with the reason and what is needed.

## Tagging tests with acceptance IDs

```python
import pytest


@pytest.mark.acceptance("AC-S3-14")
def test_final_mean_is_not_peak_aligned(): ...
```

`tests/conftest.py` copies these IDs into the JUnit XML so the gate can trace them.

Shared helpers for a stage's tests go in a uniquely named module (`tests/stage_NN/stageN_support.py`,
listed under `known-local-folder` in `pyproject.toml`). Never `from conftest import ...`: every
stage folder has a `conftest.py`, and the import silently resolves to whichever was loaded first.

## Environment notes

- Python 3.12, dependencies managed by `uv` (`uv sync`). The lockfile is `uv.lock`.
- `hvsrpy` (GPL-3.0) is in the `reference` dependency group and is used only as a numerical
  oracle in tests. Shipped code must never import it. The Docker image installs without it.
- Tests set `NUMBA_CACHE_DIR` and `MPLCONFIGDIR` to temporary directories (hvsrpy uses numba).
- Numerical threads are pinned (`OMP_NUM_THREADS=1` etc.) by the web worker to avoid
  oversubscription; the CLI respects whatever the user sets.

## Non-negotiable scientific rules (short form)

- SDOF ranking uses residual R-squared in log amplitude on a fixed 129-point grid in
  `x = ln(f/fp)` spanning `[-ln 2, ln 2]`, with precomputed peak-normalized templates. No online
  regression, no amplitude/frequency fitting, no scoring outside `[fp/2, 2 fp]`.
- Peak alignment is ONLY for template scoring. Final means are unweighted geometric means of the
  original observed per-window spectra on the common physical Hz grid.
- SESAME is reporting only. Toggling it must not change any scientific array.
- One plotted band: "Estimated 95% pointwise CI for the selected-window mean"; nominal and
  selection-conditional until calibrated.
- Unavailable numbers are `null` plus a reason, never zero, NaN, or Infinity in JSON.

## Resuming work

Read the "Next action" section at the bottom of `docs/implementation-status.md`, then run
`uv run python scripts/gate.py --all` to see which acceptance IDs currently fail.
