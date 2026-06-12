"""Persistence layer (Milestone 2) — Postgres/TimescaleDB.

``pool.py`` builds the asyncpg connection pool; ``repository.py`` holds one
repository class per table in the approved schema (see
``alembic/versions/0001_initial_schema.py``). Repositories are constructed
around any object exposing the asyncpg ``Pool``/``Connection`` execute API
(``execute``, ``fetch``, ``fetchrow``, ``executemany``), so tests can inject a
fake pool with no database.
"""

from __future__ import annotations

from .pool import DBPool, create_pool
from .repository import (
    CandleRepository,
    IndicatorRepository,
    MarketEventRepository,
    MarketMemoryRepository,
    MarketReportRepository,
    SignalRepository,
    SymbolRepository,
    TelegramAlertRepository,
    TierChangeRepository,
)

__all__ = [
    "DBPool",
    "create_pool",
    "CandleRepository",
    "IndicatorRepository",
    "MarketEventRepository",
    "MarketMemoryRepository",
    "MarketReportRepository",
    "SignalRepository",
    "SymbolRepository",
    "TelegramAlertRepository",
    "TierChangeRepository",
]
