# Quotas and resource limits

All numbers below are initial operational defaults to benchmark, not scientific standards. Every
one is an environment variable (`HVSR_` + upper-case name, see `src/hvsr_web/settings.py`).

## Contributor quota

- Public reading and downloading are anonymous. Uploading needs an administrator-issued upload
  token (`hvu_...`), sent only as `Authorization: Bearer`. Only its SHA-256 is stored; private
  e-mail and notes live only in the `contributors` table and never appear in public documents.
  Requests with credential-like query parameters are rejected.
- Budget: `HVSR_MONTHLY_QUOTA_BYTES` per contributor and UTC calendar month (default 1 GiB);
  administrators can set a per-contributor override.
- Charging policy (one accepted submission = one charge):
  - CSV and CSV.GZ: the uncompressed CSV bytes, so compression is not a quota bypass.
  - MiniSEED: the larger of the file size and the decoded N/E/Z samples at native width
    (int32: 4 bytes, float64: 8 bytes). Steim compression is not a bypass either.
  - Plus uploaded StationXML and photo bytes.
- Stored bytes (after preservation) and incoming transfer bytes are metered separately on each
  ledger entry; they are reported, not charged.
- The ledger is atomic: a reservation is a single conditional `UPDATE` (`reserved + committed +
  n <= limit`), so concurrent submissions cannot exceed the budget. The charge becomes committed in
  the same transaction that publishes the measurement.
- Retries with the same idempotency key return the accepted measurement and are not charged again.
  A failed submission releases its reservation; a crashed one is released by the sweeper.
- Removing or purging a measurement does not refund the allowance.
- When the budget is exhausted (HTTP 429) the response says how many bytes remain and suggests
  waiting for the next UTC month, splitting the submission across months, or asking an
  administrator for a quota expansion. Recordings are never shortened, decimated, or discarded to
  fit a budget.

## Guest uploads

Disabled by default (`HVSR_GUEST_UPLOADS_ENABLED`). When enabled, all guests share one
best-effort, abuse-limited monthly pool (`HVSR_GUEST_POOL_MONTHLY_BYTES`). It is not per-person
metering: an IP address or a browser cookie does not identify a person, and the database has no
column for either.

## Input ceilings

| Setting | Default | Enforced at |
|---|---|---|
| `max_request_bytes` | 128 MiB | before parsing; HTTP 413 |
| `max_decoded_bytes` | 768 MiB | while decompressing |
| `max_decompression_ratio` | 200:1 (checked once 8 MiB is decoded) | while decompressing |
| `max_samples_per_channel` | 8,640,000 | readers |
| `max_channels` | 12 | MiniSEED reader |
| `max_files` | 6 | submission |
| `max_duration_s` | 172,800 s | readers |

Every ceiling surfaces as `LimitExceeded` with a `*_limit` code before any quota is charged.

## Compute ceilings

| Setting | Default | Meaning |
|---|---|---|
| `max_candidate_windows` | 20,000 | per run, from duration, window length, and step |
| `max_template_comparisons` | 2e8 | candidate windows x templates, summed over runs, per plausible peak family |
| `max_bootstrap_replicates` | 2,000 | per run |
| `max_parameter_runs` | 2 | run A plus robustness run B |
| `max_concurrent_jobs` | 1 | live job leases |
| `job_lease_s`, `job_max_attempts` | 900 s, 3 | lease length and retry bound |
| `job_wall_time_s`, `job_cpu_time_s` | 900 s | configured; enforced by process isolation in Stage 9 |
| `max_plot_pixels` | 16,000,000 | PNG width x height in pixels (about 64 MB of RGBA), for published figures and re-renders |
| `max_window_spectra_cells` | 2,000,000 | candidate windows x valid frequencies in an on-request per-window spectra CSV |
| `max_plot_variants_per_run` | 50 | stored custom-size cosmetic plot variants per run |

The estimate is an upper bound computed from the stored recording summary before the job is
queued, and the worker re-checks it against the current settings. An over-budget request is
refused with the estimate and these options:
- increase the window step;
- turn off the robustness comparison;
- reduce bootstrap replicates or the figure size and resolution;
- process offline with the command-line tool;
- request a larger compute allowance.

Records are never silently shortened to fit. Actual CPU and wall time are recorded per job in
`compute_usage`.

Cosmetic re-renders (`POST /api/v1/runs/{run_id}/plots`) need an upload token and never rerun
science. Responses when a limit is exceeded:
- a PNG over `max_plot_pixels` returns 422 `plot_pixels`;
- a new custom-size variant beyond `max_plot_variants_per_run` returns 429 `plot_variant_limit`.

Variants at the default size and resolution are not capped: there are finitely many (selection x
overlays x format x layout). The results page only ever creates those. The cap is checked before
rendering, not atomically, so simultaneous requests can exceed it by the number of such requests.
