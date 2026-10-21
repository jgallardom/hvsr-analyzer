# Backup and restore

Code: `src/hvsr_web/backup.py`. Demonstrated by `tests/stage_07/test_backup.py` (AC-S7-10).

## What a backup contains

```
backup/
  backup.json          format, creation time, software version, schema revision,
                       row count and SHA-256 per table, SHA-256 and size per object
  db/<table>.jsonl     every row of every table, one canonical JSON object per line
  objects/<key>        every committed object (staging/ is excluded)
```

All tables are read inside one transaction, so the rows are a consistent snapshot. Objects are
copied afterwards; committed objects are immutable and are written before the rows that reference
them, so every referenced object is present.

## Restore

`restore_backup(ctx, path)`:

1. Verifies every table file and object against `backup.json`; any mismatch aborts.
2. Requires the target database to be migrated to the same schema revision as the backup.
3. Requires the target database to be empty. It never merges, overwrites, or deletes data.
4. Writes the objects (identical content already present is accepted; different content under the
   same key aborts) and inserts all rows in one transaction.
5. Re-reads every table and object and compares checksums with the backup.

After a restore, downloads, tokens (hashes), quota ledgers, idempotency keys, record-code counters,
jobs, and moderation history continue as before.

## Operating procedure

1. `create_backup(ctx, Path("/backups/hvsr-YYYYMMDD"))` on a schedule.
2. Copy the directory off the host. A backup on the same disk is not a backup.
3. Test a restore into a scratch database and store regularly; the test suite does this on every
   gate run.
4. Destructive migrations (downgrades) are refused unless
   `HVSR_ALLOW_DESTRUCTIVE_MIGRATIONS=yes-i-have-a-verified-backup` is set. Existing measurements
   are never migrated destructively without that explicit authorization.

Managed PostgreSQL and S3 also offer their own snapshots and versioning; this format is the
portable, provider-independent path and the one that is tested.
