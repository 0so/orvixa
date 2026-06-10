"""Asyncpg connection pool factory.

A thin wrapper so the rest of the codebase depends on a small ``DBPool``
``Protocol`` (the subset of :class:`asyncpg.Pool` the repositories use)
rather than ``asyncpg`` directly — this is what lets tests inject a fake pool
with no database, the same pattern M1 used for the WebSocket connector.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import asyncpg

from ..config import Settings


class DBPool(Protocol):
    """The subset of :class:`asyncpg.Pool` the repository layer needs."""

    async def execute(self, query: str, *args: Any) -> str: ...

    async def executemany(self, query: str, args: Sequence[Sequence[Any]]) -> None: ...

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]: ...

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None: ...

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def close(self) -> None: ...


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """Create the production asyncpg pool from :class:`Settings`."""
    return await asyncpg.create_pool(
        dsn=settings.postgres_dsn,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
    )
