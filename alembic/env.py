"""Alembic environment — runs migrations using DATABASE_URL from env."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL must point to the owner role (postgres) so migrations can
# CREATE/DROP tables and grant column-level privileges to the app roles.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is required for Alembic migrations")
config.set_main_option("sqlalchemy.url", DATABASE_URL)

# We do not use SQLAlchemy ORM autogenerate. Schema lives in hand-written
# revisions (alembic/versions/*.py) so the canonical Pydantic models in
# shared.models are the only source of inter-agent contracts and the SQL
# schema is a separate, auditable artifact.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
