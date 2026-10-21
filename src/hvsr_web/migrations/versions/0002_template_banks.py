"""template banks (additive: two new tables, no change to existing rows)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 21:25:28.213309
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "template_banks",
        sa.Column("bank_id", sa.String(length=82), nullable=False),
        sa.Column("family", sa.String(length=8), nullable=False),
        sa.Column("bank_version", sa.String(length=64), nullable=False),
        sa.Column("n_templates", sa.Integer(), nullable=False),
        sa.Column("license", sa.Text(), nullable=False),
        sa.Column("data_sha256", sa.String(length=64), nullable=False),
        sa.Column("data_bytes", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("manifest_key", sa.String(length=300), nullable=False),
        sa.Column("data_key", sa.String(length=300), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=36), nullable=False),
        sa.Column("uploaded_utc", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["uploaded_by"], ["admins.id"]),
        sa.PrimaryKeyConstraint("bank_id"),
    )
    op.create_table(
        "bank_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bank_id", sa.String(length=82), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("created_utc", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["bank_id"], ["template_banks.bank_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("bank_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_bank_events_bank_id"), ["bank_id"], unique=False)


def downgrade() -> None:
    from hvsr_web.db import destructive_migrations_authorized

    if not destructive_migrations_authorized():
        raise RuntimeError(
            "Refusing a downgrade that drops uploaded template-bank records needed for replay. Take and "
            "verify a backup, then set HVSR_ALLOW_DESTRUCTIVE_MIGRATIONS=yes-i-have-a-verified-backup."
        )
    with op.batch_alter_table("bank_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_bank_events_bank_id"))
    op.drop_table("bank_events")
    op.drop_table("template_banks")
