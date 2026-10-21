# Storage, artifacts, and analysis jobs

Operational design for Stage 7 (spec sections 1, 3, 4, 13.2-13.3, 14). Code: `src/hvsr_web/`.

## Components

| Concern | Module | Notes |
|---|---|---|
| Settings | `settings.py` | Every secret and operational limit is an `HVSR_*` environment variable. |
| Metadata database | `db.py`, `migrations/` | SQLAlchemy 2 + Alembic. SQLite (development, single host) or PostgreSQL. |
| Object store | `storage.py` | Local filesystem or any S3-compatible API, one bucket, logical prefixes. |
| Submissions | `submissions.py` | Validate, reserve quota, stage, commit objects, one database commit. |
| Jobs | `jobs.py`, `analysis_jobs.py` | Durable job table, leases, fencing tokens, idempotent publication. |
| Manifests | `manifests.py` | Public index of every artifact with stable URLs. |
| HTTP API | `api.py` | JSON endpoints; the web pages arrive in Stage 8. |

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
| CSV, default | original only | "Canonical recording" and "Normalized waveform CSV" are `on_demand` (generated deterministically from the original; no second copy of the samples) |
| CSV, canonical-only (explicit opt-in) | canonical MiniSEED + sidecar + exact residual columns | "Original CSV (not retained)" is `unavailable` with its SHA-256 for reference; "Canonical recording" is available; "Reconstructed CSV" is `on_demand` |

`recording_from_canonical` rebuilds the parsed recording from canonical storage; its
`recording_hash` equals that of parsing the original CSV (tested for all three CSV layouts), so
an analysis of a canonical-only measurement is computed from the same samples.

## Stable links and signed URLs

Manifests list `/api/v1/artifacts/{artifact_id}` for every artifact, never a signed URL. Each
request to that endpoint issues a fresh short-lived URL (`HVSR_SIGNED_URL_TTL_S`, default 300 s):
an HMAC-signed `/api/v1/blobs/...` URL for the local store or an S3 presigned URL (SigV4). An
expired signed URL returns 403; requesting the stable link again works. Removed measurements return
410 with the removal notice; `on_demand` artifacts return 409 until Stage 8 adds generation.

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

Per-job CPU and wall-time ceilings (`HVSR_JOB_CPU_TIME_S`, `HVSR_JOB_WALL_TIME_S`) are configured
here; process isolation that enforces them is Stage 9 work.
