"""Opt-in end-to-end test against a real Postgres/TimescaleDB instance.

Skipped unless ``RUN_DB_TESTS=1`` (mirrors M1's ``RUN_NET_TESTS`` gate for
live-network tests) — there's no Docker/Postgres in the unit-test sandbox, so
``make test`` stays DB-free and green. To run for real:

    docker compose -f docker-compose.dev.yml up -d postgres
    alembic upgrade head
    RUN_DB_TESTS=1 PYTHONPATH=src pytest tests/test_db_integration.py -q

This applies the ``0001_initial_schema`` migration's effects (assumed already
applied via ``alembic upgrade head``), seeds the symbol registry, inserts a
batch of candles through the repository layer, and reads them back.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from orvixa.config import get_settings
from orvixa.db import CandleRepository, SymbolRepository, create_pool
from orvixa.db.models import CandleRow, SymbolRow

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_TESTS") != "1",
    reason="set RUN_DB_TESTS=1 against a migrated Postgres/TimescaleDB instance",
)


async def test_candle_round_trip() -> None:
    settings = get_settings()
    pool = await create_pool(settings)
    try:
        symbol_repo = SymbolRepository(pool)
        candle_repo = CandleRepository(pool)

        symbol_id = await symbol_repo.upsert(
            SymbolRow(symbol="BTCUSDT", base="BTC", klass="core", tier=0)
        )
        assert symbol_id > 0

        row = CandleRow(
            symbol_id=symbol_id,
            ts=datetime.now(tz=UTC).replace(microsecond=0),
            open=100.0,
            high=110.0,
            low=90.0,
            close=105.0,
            volume=10.0,
            quote_volume=1_000.0,
            trades=5,
            taker_buy_volume=4.0,
        )
        inserted = await candle_repo.insert_batch([row])
        assert inserted == 1

        recent = await candle_repo.get_recent(symbol_id, limit=1)
        assert len(recent) == 1
        assert recent[0]["symbol_id"] == symbol_id
        assert recent[0]["ts"] == row.ts

        # Re-delivery upserts in place rather than duplicating: the row count
        # for this exact (symbol_id, interval, ts) stays at one.
        before = await pool.fetchval(
            "SELECT count(*) FROM candles WHERE symbol_id = $1 AND interval = $2 AND ts = $3",
            symbol_id,
            row.interval,
            row.ts,
        )
        await candle_repo.insert_batch([row])
        after = await pool.fetchval(
            "SELECT count(*) FROM candles WHERE symbol_id = $1 AND interval = $2 AND ts = $3",
            symbol_id,
            row.interval,
            row.ts,
        )
        assert before == 1
        assert after == 1
    finally:
        await pool.close()
