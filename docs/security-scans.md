# Security scans

Two automated scans run on every change in CI and can be rerun locally:

```bash
uv run python scripts/security_scan.py            # pip-audit (needs network) + bandit
uv run python scripts/security_scan.py --offline  # bandit only; keeps the last pip-audit record
```

The script writes its evidence to `docs/evidence/stage9/`:

| File | Content |
|---|---|
| `security-scan.json` | Summary: tool versions, dates, SHA-256 of `uv.lock`, counts, vulnerabilities |
| `pip-audit-pypi.json`, `pip-audit-osv.json` | Full pip-audit reports against PyPI advisories and OSV |
| `bandit.json` | Every bandit finding (severity, confidence, test, file, line) |

The AC-S9-03 test (`tests/stage_09/test_security.py`) fails if the recorded dependency scan was
made against a different `uv.lock`, if any vulnerability is recorded, if bandit now reports a
medium or high finding, or if a low finding is not justified in the table below.

## Dependency vulnerability scan (pip-audit)

- Scope: every package pinned in `uv.lock`, across all dependency groups (runtime, dev, and the
  GPL `reference` group used only by tests), exported with hashes and audited with
  `--require-hashes --disable-pip`, so the audited versions are exactly the locked ones.
- Sources: PyPI advisory data and the OSV database, queried separately.
- Result on 2026-10-21 (pip-audit 2.10.1, lock SHA-256 `d9e837d2…`): 130 pinned packages, 128
  audited, **no known vulnerabilities** in either source. The two packages not audited apply only
  to other platforms (`colorama` on Windows, `tzdata` on Windows/Emscripten).
- A clean result means no *published* advisory matched these versions on that date. It is not a
  guarantee; rerun the scan whenever `uv.lock` changes (the test enforces this) and periodically.
- Network note: the development sandbox has no DNS. The recorded scan was run with network access
  outside that sandbox. The CI workflow (`.github/workflows/ci.yml`) reruns both scans on every
  push and weekly. Its security job passed on the first GitHub runs (2026-10-21; see
  `docs/implementation-status.md`).

## Static security scan (bandit)

- Scope: `src/`, `packages/hvsr_core/src/`, `scripts/` (tests are excluded: they use `assert` and
  deliberately hostile inputs). No `# nosec` suppressions are used.
- Result on 2026-10-21 (bandit 1.9.4, 15,591 lines): **0 high, 0 medium, 23 low**, all justified
  below.

### Fixed during Stage 9

| Finding | Where | Resolution |
|---|---|---|
| B314 (medium): XML parsed with `xml.etree` | `scripts/gate.py` reading pytest's JUnit report | Now parsed with `defusedxml`, although the file is produced locally by pytest. |

### Justified low findings

| Test | File | Why it is acceptable |
|---|---|---|
| B404 import subprocess, B603 subprocess call | `packages/hvsr_core/src/hvsr_core/analysis.py` | Runs `git rev-parse HEAD` and `git status` for provenance: absolute executable from `shutil.which`, fixed argument list, no shell, 5 s timeout. `HVSR_GIT_COMMIT` replaces it in containers. |
| B404, B603 | `packages/hvsr_core/src/hvsr_core/research/validation_protocol.py` | Offline validation harness runs `git` with fixed arguments to verify the committed protocol lock. Never reachable from the web service. |
| B404, B603, B607 partial path | `scripts/gate.py` | Developer and CI gate: runs `git` (looked up on `PATH`) and `python -m pytest` with fixed arguments, no shell. Not shipped as a service entry point. |
| B404, B603 | `scripts/security_scan.py` | Runs `uv`, `pip-audit`, and `bandit` with fixed arguments, no shell. |
| B404, B603 | `scripts/smoke.py` | Startup smoke test: runs `uv sync`, the server, the admin CLI, and `docker` from absolute paths (`shutil.which`, `sys.executable`) with argument lists, no shell, and timeouts. The only operator-supplied values are its own command-line options (image name, port). Not shipped as a service entry point. |
| B112 try/except/continue | `src/hvsr_web/submissions.py` (`_clean_staging`) | Deleting a staged upload is best effort; a failed deletion is retried by `sweep-staging`, and the database state only becomes `cleaned` once the object is gone. |

## Findings from review beyond the scanners

Stage 9 also reviewed the input paths by hand and with fuzzing (`tests/stage_09/test_malformed.py`):

| Issue | Severity | Resolution |
|---|---|---|
| StationXML guard looked for `<!DOCTYPE`/`<!ENTITY` as ASCII bytes. A UTF-16 document (or a DOCTYPE after a long prolog comment) passed the guard, and lxml expanded internal entities. External entities were not resolved (lxml's default), so no file disclosure; the risk was entity-expansion resource exhaustion. | Medium | `stationxml._reject_dtd` parses with entity resolution, DTD loading, and network access disabled and rejects any DOCTYPE in any encoding. Regression test covers UTF-8/UTF-16, long prologs, nested expansion, and an external DTD. |
| CSV parsing held every line and every sample as Python objects: about 760 MB in the web process for a 2 h, 200 Hz upload, extrapolating to several GB at the configured sample cap. | Medium (memory exhaustion) | Lines are streamed (`iter_lines`, identical boundaries to `str.splitlines`, property-tested) and samples are stored in packed arrays. Peak for the same upload is now about 290 MB; see `docs/performance.md`. Results are unchanged (old and new readers agree on values and errors). |
| A CSV line over the csv module's field limit, and a non-UTF-8 JSON body, returned HTTP 500. | Low | Both now return 422 with a clear reason. |

## Controls verified by tests

- Uploaded template banks are data only (JSON manifest + `.npz` loaded with `allow_pickle=False`);
  a pickled payload that would run a command is rejected and the command never runs.
- Only administrator tokens can upload or activate banks; upload tokens never can.
- Decompression bombs, oversized requests, path traversal in blob keys, and tokens in query strings
  are rejected without storing anything.
