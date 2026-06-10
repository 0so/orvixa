"""Shared API dependencies — the read-only connection pool and its lifespan.

The API opens its own asyncpg pool (separate from the ingest/analytics
runners) with a ``jsonb`` decode codec so ``components``/``payload``/
``snapshot`` columns come back as real dicts rather than raw JSON text. It is
used for ``SELECT`` only; nothing here writes.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from ..config import Settings


async def _init_connection(conn: asyncpg.Connection) -> None:
    # Decode jsonb to Python objects so endpoints return structured JSON.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_readonly_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        dsn=settings.postgres_dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        init=_init_connection,
    )


def record_to_dict(record: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(record) if record is not None else None


def records_to_list(records: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in records]
