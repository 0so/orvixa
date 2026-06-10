"""Alembic environment — async engine driven by ORVIXA's ``Settings``.

The DSN comes from :func:`orvixa.config.get_settings` (env vars / ``.env``,
the same source the application pool uses), not from ``alembic.ini``, so
migrations and the app always target the same database. ``postgresql://`` is
rewritten to ``postgresql+asyncpg://`` for SQLAlchemy's async engine; the
migrations themselves are raw SQL via ``op.execute``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from orvixa.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _async_dsn() -> str:
    dsn = get_settings().postgres_dsn
    if dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
    return dsn


def run_migrations_offline() -> None:
    """Run migrations without a DB connection, emitting SQL to stdout."""
    context.configure(
        url=_async_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database via an async engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _async_dsn()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
