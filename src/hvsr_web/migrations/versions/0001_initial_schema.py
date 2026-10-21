"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-20 19:42:19.847653
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admins",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("private_name", sa.String(length=200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_sha256"),
    )
    op.create_table(
        "contributors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=True),
        sa.Column("private_email", sa.String(length=320), nullable=True),
        sa.Column("private_note", sa.Text(), nullable=True),
        sa.Column("monthly_quota_bytes", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_sha256"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("contributor_id", sa.String(length=36), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_jobs_state"), ["state"], unique=False)

    op.create_table(
        "record_code_counters",
        sa.Column("base_code", sa.String(length=32), nullable=False),
        sa.Column("last_n", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("base_code"),
    )
    op.create_table(
        "compute_usage",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("contributor_id", sa.String(length=36), nullable=True),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("estimate", sa.JSON(), nullable=False),
        sa.Column("cpu_s", sa.Float(), nullable=True),
        sa.Column("wall_s", sa.Float(), nullable=True),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributors.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("compute_usage", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_compute_usage_contributor_id"), ["contributor_id"], unique=False)

    op.create_table(
        "measurements",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("record_code", sa.String(length=40), nullable=False),
        sa.Column("base_code", sa.String(length=32), nullable=False),
        sa.Column("suffix_n", sa.Integer(), nullable=False),
        sa.Column("geohash", sa.String(length=12), nullable=False),
        sa.Column("decimal_year", sa.Float(), nullable=False),
        sa.Column("decimal_year_source", sa.String(length=20), nullable=False),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.Column("contributor_id", sa.String(length=36), nullable=False),
        sa.Column("submission_key", sa.String(length=128), nullable=False),
        sa.Column("moderation_status", sa.String(length=16), nullable=False),
        sa.Column("original_bytes_retained", sa.Boolean(), nullable=False),
        sa.Column("original_bytes_note", sa.Text(), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("site_group_id", sa.String(length=64), nullable=True),
        sa.Column("licensing", sa.JSON(), nullable=False),
        sa.Column("parse_options", sa.JSON(), nullable=False),
        sa.Column("recording_summary", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributors.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contributor_id", "submission_key", name="uq_submission_idem"),
        sa.UniqueConstraint("record_code"),
    )
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_measurements_base_code"), ["base_code"], unique=False)
        batch_op.create_index(batch_op.f("ix_measurements_contributor_id"), ["contributor_id"], unique=False)

    op.create_table(
        "quota_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("contributor_id", sa.String(length=36), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("logical_bytes", sa.Integer(), nullable=False),
        sa.Column("stored_bytes", sa.Integer(), nullable=True),
        sa.Column("transfer_bytes", sa.Integer(), nullable=True),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.Column("updated_utc", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributors.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contributor_id", "idempotency_key", name="uq_quota_idem"),
    )
    with op.batch_alter_table("quota_entries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_quota_entries_contributor_id"), ["contributor_id"], unique=False)

    op.create_table(
        "quota_periods",
        sa.Column("contributor_id", sa.String(length=36), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("limit_bytes", sa.Integer(), nullable=False),
        sa.Column("reserved_bytes", sa.Integer(), nullable=False),
        sa.Column("committed_bytes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributors.id"],
        ),
        sa.PrimaryKeyConstraint("contributor_id", "period"),
    )
    op.create_table(
        "staged_uploads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("submission_key", sa.String(length=128), nullable=False),
        sa.Column("contributor_id", sa.String(length=36), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributors.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_table(
        "analysis_runs",
        sa.Column("run_id", sa.String(length=40), nullable=False),
        sa.Column("measurement_id", sa.String(length=36), nullable=False),
        sa.Column("metadata_revision", sa.Integer(), nullable=False),
        sa.Column("science_key", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("primary_run_id", sa.String(length=40), nullable=True),
        sa.Column("comparison", sa.JSON(), nullable=True),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(
            ["measurement_id"],
            ["measurements.id"],
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    with op.batch_alter_table("analysis_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_analysis_runs_measurement_id"), ["measurement_id"], unique=False)

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("measurement_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("blob_key", sa.String(length=255), nullable=True),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("stored_sha256", sa.String(length=64), nullable=True),
        sa.Column("stored_encoding", sa.String(length=16), nullable=True),
        sa.Column("availability", sa.String(length=16), nullable=False),
        sa.Column("availability_reason", sa.Text(), nullable=True),
        sa.Column("generated_by", sa.JSON(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("method", sa.String(length=32), nullable=True),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["measurement_id"],
            ["measurements.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_artifacts_measurement_id"), ["measurement_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_artifacts_run_id"), ["run_id"], unique=False)

    op.create_table(
        "metadata_revisions",
        sa.Column("measurement_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("author_attribution", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["measurement_id"],
            ["measurements.id"],
        ),
        sa.PrimaryKeyConstraint("measurement_id", "revision"),
    )
    op.create_table(
        "moderation_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("measurement_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.Column("notice", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["measurement_id"],
            ["measurements.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("moderation_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_moderation_events_measurement_id"), ["measurement_id"], unique=False
        )


def downgrade() -> None:
    from hvsr_web.db import destructive_migrations_authorized

    if not destructive_migrations_authorized():
        raise RuntimeError(
            "Refusing a destructive downgrade that drops all measurement metadata. Take and verify a "
            "backup, then set HVSR_ALLOW_DESTRUCTIVE_MIGRATIONS=yes-i-have-a-verified-backup."
        )
    with op.batch_alter_table("moderation_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_moderation_events_measurement_id"))

    op.drop_table("moderation_events")
    op.drop_table("metadata_revisions")
    with op.batch_alter_table("artifacts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_artifacts_run_id"))
        batch_op.drop_index(batch_op.f("ix_artifacts_measurement_id"))

    op.drop_table("artifacts")
    with op.batch_alter_table("analysis_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_analysis_runs_measurement_id"))

    op.drop_table("analysis_runs")
    op.drop_table("staged_uploads")
    op.drop_table("quota_periods")
    with op.batch_alter_table("quota_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_quota_entries_contributor_id"))

    op.drop_table("quota_entries")
    with op.batch_alter_table("measurements", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_measurements_contributor_id"))
        batch_op.drop_index(batch_op.f("ix_measurements_base_code"))

    op.drop_table("measurements")
    with op.batch_alter_table("compute_usage", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_compute_usage_contributor_id"))

    op.drop_table("compute_usage")
    op.drop_table("record_code_counters")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_jobs_state"))

    op.drop_table("jobs")
    op.drop_table("contributors")
    op.drop_table("admins")
