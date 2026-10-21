# Backup and restore

Code: `src/hvsr_web/backup.py`; operator commands in `src/hvsr_web/admin.py`.
Tests:
- `tests/stage_07/test_backup.py` (AC-S7-10): library round trip.
- `tests/stage_09/test_admin.py` (AC-S9-08): the commands below, run as an operator would.
- `tests/stage_09/test_postgres.py` (AC-S9-14): PostgreSQL to PostgreSQL, PostgreSQL to SQLite, and
  SQLite to PostgreSQL.

## What a backup contains

```
backup/
  backup.json          format, creation time, software version, schema revision,
                       row count and SHA-256 per table, SHA-256 and size per object
  db/<table>.jsonl     every row of every table, one canonical JSON object per line
  objects/<key>        every committed object (staging/ is excluded)
```

All tables are read inside one transaction, so the rows are a consistent snapshot. Objects are
copied afterwards. Committed objects are immutable and are written before the rows that reference
them, so every referenced object is present. Rows are written in primary-key order, sorted by the
application rather than by the database, so a backup taken on one database engine verifies on
another whatever its text collation.

The backup holds private data: contributor e-mail addresses, token hashes, and quota ledgers.
Store it with the same care as the production database.

## Commands

Run these where the service's environment (`HVSR_DATABASE_URL`, `HVSR_OBJECT_STORE`, bucket
settings, ...) is set. On Render that is a shell on the web or worker service.

```bash
python -m hvsr_web.admin backup create /backups/hvsr-20261021   # snapshot rows + committed objects
python -m hvsr_web.admin backup verify /backups/hvsr-20261021   # re-check every checksum
python -m hvsr_web.admin backup restore /backups/hvsr-20261021  # into an EMPTY, migrated database
```

Each command prints JSON. It exits with status 1 and an `error:` line on failure, for example a
checksum mismatch or a non-empty target.

## Restore

`restore`:

1. Verifies every table file and object against `backup.json`; any mismatch aborts before writing.
2. Requires the target database to be migrated to the backup's schema revision
   (`python -m hvsr_web.admin migrate`).
3. Requires the target database to be empty. It never merges, overwrites, or deletes data.
4. Writes the objects (identical content already present is accepted; different content under the
   same key aborts) and inserts all rows in one transaction.
5. Re-reads every table and object and compares checksums with the backup.

After a restore, downloads, tokens (hashes), quota ledgers, idempotency keys, record-code counters,
jobs, uploaded template banks, and moderation history continue as before.

## Operating procedure

1. Schedule `backup create` (for example daily) into a dated directory.
2. Copy the directory off the host, for example to a bucket in another account or provider, then
   delete the local copy. A backup on the same disk is not a backup. Render service disks are
   ephemeral, so a backup left on the service disk is lost at the next deploy.
3. Keep several generations, for example 7 daily and 4 weekly, and include at least one taken
   before any migration.
4. Rehearse a restore regularly: create a scratch PostgreSQL database, point `HVSR_DATABASE_URL`
   and an empty object-store prefix or bucket at it, run `migrate` and `restore`, then open a
   measurement page. The test suite runs the same steps on every gate run.
5. Destructive migrations (downgrades) are refused unless
   `HVSR_ALLOW_DESTRUCTIVE_MIGRATIONS=yes-i-have-a-verified-backup` is set. Existing measurements
   are never migrated destructively without that explicit authorization.

### Moving from SQLite to PostgreSQL

`backup create` on the SQLite site, then `migrate` and `backup restore` against the new
PostgreSQL URL with the same object store (or a copy of it). Tested by
`test_analysis_pages_and_backup_restore_across_postgres_and_sqlite`.

### Provider snapshots

Managed PostgreSQL (Render) and S3-compatible stores (Cloudflare R2) have their own backup
features. Check the current plan's terms before relying on them; they are not tested here. This
JSON Lines format is the portable, provider-independent path, and it is the one that is tested.
