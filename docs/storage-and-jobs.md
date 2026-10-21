# Storage, artifacts, and analysis jobs

Operational design for Stages 7 to 9 (spec sections 1, 3, 4, 13.2-13.3, 14). Code: `src/hvsr_web/`.

## Components

| Concern | Module | Notes |
|---|---|---|
| Settings | `settings.py` | Every secret and operational limit is an `HVSR_*` environment variable. |
| Metadata database | `db.py`, `migrations/` | SQLAlchemy 2 + Alembic. SQLite (development, single host) or PostgreSQL. |
| Object store | `storage.py` | Local filesystem or any S3-compatible API, one bucket, logical prefixes. |
| Submissions | `submissions.py` | Validate, reserve quota, stage, commit objects, one database commit. |
| Jobs | `jobs.py`, `analysis_jobs.py` | Durable job table, leases, fencing tokens, idempotent publication. |
| Manifests | `manifests.py` | Public index of every artifact with stable URLs. |
| HTTP API | `api.py` | JSON endpoints, the web pages, and static files under `/static`. |
| Web pages | `pages.py`, `templates/`, `static/` | Server-rendered HTML; JavaScript only for upload, polling, and on-request downloads. |
| Run artifacts | `publication.py` | Analysis JSON, CSV tables, and figures of a run, written once at publication. |
| On-request generation | `ondemand.py` | Canonical/reconstructed recordings, normalized waveform, per-window spectra. |
| Read side, cosmetic re-render | `runs.py` | New plot artifacts from the stored analysis JSON; no science is rerun. |
| Worker | `worker.py`, `isolation.py` | Separate process (`HVSR_EMBEDDED_WORKER=false`) or a thread in the web process; each job in its own limited child process. |
| Template banks | `banks.py` | Administrator upload, activation, and materialization of data-only banks. |
| Operator commands | `admin.py` | Migrations, tokens, backups, banks, staging sweep. |

## Object layout and immutability

Committed objects are content-addressed: `{category}/sha256/{aa}/{sha256}` with categories
`recordings`, `metadata`, `analyses`, `plots`, `templates`, `photos`. Canonical-only recordings
use `recordings/measurements/{measurement_id}/...`; immutable science documents use
`analyses/science/{science_key}.json`.

- A committed key is never overwritten with different bytes (`ImmutableObjectError`). Writing
  identical bytes again is a no-op, which makes retries safe.
- On S3 the adapter writes with `If-None-Match: *` and an `x-amz-checksum-sha256`. Providers that
  ignore conditional writes lose the overwrite protection; content-addressed keys still cannot
  change meaning, but `analyses/science/` keys could. Use a provider that supports conditional
  writes (AWS S3 does) or keep one worker.
- `staging/` is the only mutable prefix. The staging sweeper (`sweep_staging`) only lists and
  deletes keys under `staging/`; committed data are out of its reach by construction.
- Every committed object is read back and its SHA-256 compared before the database commit that
  publishes it.

## Submission order (crash safety)

1. Validate consent, limits, and parse the files (no storage, no charge).
2. Reserve quota in its own transaction (atomic conditional `UPDATE`).
3. Stage the raw uploads under `staging/{submission}/` with `staged_uploads` rows.
4. Write and verify the committed objects.
5. One transaction: allocate the record code, insert the measurement, metadata revision 1,
   artifacts, commit the quota charge, mark staging rows committed.
6. Delete the staged objects.

A failure before step 5 deletes the staged objects and releases the reservation. A crash leaves
staged objects and a `reserved` ledger entry; the sweeper deletes the former and releases the
latter after `HVSR_STAGING_TTL_S`, never touching an accepted submission. Content-addressed objects
written in step 4 by a failed submission stay unreferenced; they are never deleted automatically
because another measurement may share them.

## Original, canonical, and reconstructed data

| Upload | Stored | Labels |
|---|---|---|
| CSV | deterministic gzip of the exact bytes | "Original CSV" (downloads decompress and verify to the original SHA-256) |
| CSV.GZ | the uploaded bytes | "Original CSV.GZ" |
| MiniSEED | the uploaded bytes | "Original MiniSEED"; "Canonical recording (the original MiniSEED)" points at the same object |
| CSV, default | original only | "Canonical recording" and "Normalized waveform CSV.GZ" are `on_demand` (generated deterministically from the original; no second copy of the samples) |
| CSV, canonical-only (explicit opt-in) | canonical MiniSEED + sidecar + exact residual columns | "Original CSV (not retained)" is `unavailable` with its SHA-256 for reference; "Canonical recording" is available; "Reconstructed CSV" is `on_demand` |

`recording_from_canonical` rebuilds the parsed recording from canonical storage; its
`recording_hash` equals that of parsing the original CSV (tested for all three CSV layouts), so
an analysis of a canonical-only measurement is computed from the same samples.

## Stable links and signed URLs

Manifests list `/api/v1/artifacts/{artifact_id}` for every artifact, never a signed URL. Each
request to that endpoint issues a fresh short-lived URL (`HVSR_SIGNED_URL_TTL_S`, default 300 s):
an HMAC-signed `/api/v1/blobs/...` URL for the local store or an S3 presigned URL (SigV4). An
expired signed URL returns 403; requesting the stable link again works. Removed measurements return
410 with the removal notice.

Responses of the stable artifact URL:

| Availability | Response |
|---|---|
| `available` | 307 to a fresh signed URL |
| `on_demand` | 202 with `Retry-After: 5` and the `job_url` of the single generation job (idempotency key `generate:{artifact_id}`); once the job finishes, the artifact is `available` and served from storage |
| `on_demand`, generation failed permanently | 503 `{"error": "generation_failed"}` with the reason; the manifest keeps `on_demand` and its reason |
| `unavailable` (for example an original not retained) | 404 `{"error": "unavailable", "detail", "sha256"}` |
| `removed` | 410 with the removal notice |

Generation reproduces bytes from stored inputs and checks the recorded hashes. A mismatch is a
permanent failure, never a substitute. Per-window spectra are bounded by
`HVSR_MAX_WINDOW_SPECTRA_CELLS` (candidate windows x valid frequencies, default 2,000,000) and
refused with the reason above that.

## Run artifacts (Stage 8)

Per run and method, written once at publication and content-addressed:
- the analysis JSON;
- the mean-spectrum CSV;
- the per-window table as CSV and JSON (selected and rejected candidates);
- the figure as PNG, SVG, and a narrow-screen SVG.

Per run, the per-window spectra CSV.GZ is `on_demand`. Per template bank used, the bank manifest and
data are published. Derived artifacts list the analysis JSON in `source_refs`. Figures store their
recipe in `generated_by.recipe`, including `series_sha256` of the plotted series, which also
identifies the same numbers in the CSV and JSON. Each published analysis JSON embeds the immutable
licensing subset (license, attribution, source citation, record code, manifest URL). The manifest
holds the current licensing record with its modification history.

`POST /api/v1/runs/{run_id}/plots` (upload token required) renders a cosmetic variant from the
stored analysis JSON, returning 201 when new and 200 for an identical recipe. Showing individual
windows needs the per-window spectra; until they are stored the request returns 409
`window_spectra_not_stored` with that artifact's URL. Variants never replace the published figure.
They are listed in the manifest's `plots` category with `generated_by.cosmetic_rerender: true`.

Set `HVSR_URL_SIGNING_KEY` in any deployment with more than one process; without it each process
uses a random key and URLs signed by one process fail in another.

## Analysis jobs

- `request_analysis` estimates the cost from the stored recording summary, refuses requests over
  the compute ceilings with advice (see `docs/quotas-and-limits.md`), and enqueues one job per
  (measurement, metadata revision, request). Identical requests share the job.
- `claim` is one conditional `UPDATE`: at most `HVSR_MAX_CONCURRENT_JOBS` live leases, each with
  a fresh fencing token. Expired leases are reclaimable; after `HVSR_JOB_MAX_ATTEMPTS` the job is
  marked `dead` with the reason.
- The handler loads the recording from the verified original (or canonical-only storage), applies
  the pinned metadata snapshot and StationXML, and runs the analysis. Science documents are cached
  in the object store by science key, so a retry after a crash reuses them byte for byte.
- Publication is one transaction: run rows (run A and robustness run B), artifact rows with
  deterministic IDs, a compute-usage row, and the fenced job completion. A worker whose lease was
  taken over gets `LeaseLost` and its transaction rolls back; a repeated publication adds nothing.
- The science key includes the record identity block (record code, measurement, metadata
  revision, coordinates), so the same bytes submitted as two measurements, or a metadata
  correction, produce distinct runs whose documents carry the right record.
- Reporting-only changes (SESAME on or off) reuse the run and add report-variant artifacts.

## Job isolation and limits (Stage 9)

With `HVSR_JOB_ISOLATION=process` (the default) each job runs in a fresh spawned child process
(`isolation.py`):

1. The child pins every numerical thread pool to `HVSR_NUMERIC_THREADS` and claims one job.
2. It reports the claim to the parent, then sets `RLIMIT_CPU` and `RLIMIT_AS` relative to its own
   baseline after imports, and runs the handler.
3. The parent renews the lease while the child runs and kills the child at `HVSR_JOB_WALL_TIME_S`.

| Outcome | Job state |
|---|---|
| CPU, memory, or wall-time limit exceeded | failed permanently, with the limit named |
| other abnormal exit (crash, signal) | retried, up to `HVSR_JOB_MAX_ATTEMPTS`, then `dead` |
| the parent itself dies | the lease expires; another worker re-claims the job |

Publication is one fenced transaction inside the child, so a killed job never leaves a partially
published run. `inline` mode runs jobs in the worker process without these limits (tests and
debugging only).

## PostgreSQL serialization

SQLite serializes writers (`BEGIN IMMEDIATE`). PostgreSQL's READ COMMITTED does not, so three
count-then-write steps take a transaction-scoped advisory lock (`db.serialize`):

- startup migrations (web and worker may both migrate at once; the second waits and finds nothing
  to do);
- job claims (the `HVSR_MAX_CONCURRENT_JOBS` ceiling);
- quota reservations, per contributor (concurrent retries of one idempotency key).

These races were found by running the PostgreSQL tests (`tests/stage_09/test_postgres.py`, a
throwaway PostgreSQL 16 server from `pgserver`, or `HVSR_TEST_POSTGRES_URL`); the SQLite tests
could not show them. Hosted `postgres://` and `postgresql://` URLs are mapped to the installed
psycopg 3 driver.

## Worker topology and shutdown

- One service (the Render blueprint): `HVSR_EMBEDDED_WORKER=true` runs the worker loop as a
  thread of the web process; each job still runs in its own child process.
- Separate worker: `HVSR_EMBEDDED_WORKER=false` on the web service and
  `python -m hvsr_web.worker` as a second process with the same environment. Add one only when
  profiling or deployment constraints justify it.
- On SIGTERM the standalone worker lets the current job finish and then exits. The web server
  stops its embedded worker during shutdown. No new job is claimed, and a running job's process
  ends normally or at its wall-time limit before the server process exits.
- A job cut off by a forced kill is re-claimed after its lease expires (`HVSR_JOB_LEASE_S`).
  Render force-stops after `maxShutdownDelaySeconds`, which is 120 s in the blueprint.

## Template banks

Administrator-uploaded banks are stored in the object store and recorded in `template_banks`, and
copied into `HVSR_BANK_DIR` (a cache) before each analysis. See `docs/template-banks.md`.
