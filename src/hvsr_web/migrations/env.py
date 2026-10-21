"""Alembic environment. Forward migrations only by default (see hvsr_web.db.migrate)."""

from __future__ import annotations

from alembic import context

from hvsr_web.db import MIGRATION_LOCK, Base, make_engine, serialize

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        connectable = make_engine(config.get_main_option("sqlalchemy.url"))
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            # web and worker both migrate at startup; the second waits, then finds nothing to do
            serialize(connection, MIGRATION_LOCK)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
