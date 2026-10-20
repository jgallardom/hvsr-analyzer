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
