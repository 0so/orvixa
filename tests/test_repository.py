"""Repository layer against a :class:`FakePool` — no database required.

Each repository is exercised through its public methods only, asserting the
SQL shape (table name, ``ON CONFLICT``/``RETURNING`` clauses) and that
returned rows are translated into the expected Python values.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fake_pool import FakePool
from orvixa.db.models import (
    CandleRow,
    IndicatorRow,
    MarketEventRow,
    MarketMemoryRow,
    MarketReportRow,
    SignalRow,
    SymbolRow,
    TelegramAlertRow,
)
from orvixa.db.repository import (
    CandleRepository,
    IndicatorRepository,
    MarketEventRepository,
    MarketMemoryRepository,
    MarketReportRepository,
    SignalRepository,
    SymbolRepository,
    TelegramAlertRepository,
)

_TS = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


async def test_symbol_repository_upsert_and_lookup() -> None:
    pool = FakePool()
    repo = SymbolRepository(pool)

    pool.fetchrow_return = {"id": 7}
    symbol_id = await repo.upsert(SymbolRow(symbol="BTCUSDT", base="BTC", klass="core", tier=0))
    assert symbol_id == 7
    query, args = pool.fetchrow_calls[0]
    assert "INSERT INTO symbols" in query
    assert "ON CONFLICT (base) DO UPDATE" in query
    assert args[:2] == ("BTCUSDT", "BTC")

    pool.fetchval_return = 7
    assert await repo.get_id("BTC") == 7

    pool.fetchval_return = None
    assert await repo.get_id("NOPE") is None


async def test_symbol_repository_ensure_seeded() -> None:
    pool = FakePool()
    repo = SymbolRepository(pool)

    rows = [
        SymbolRow(symbol="BTCUSDT", base="BTC", klass="core", tier=0),
        SymbolRow(symbol="ETHUSDT", base="ETH", klass="core", tier=0),
    ]
    ids = iter([1, 2])

    async def fake_upsert(row: SymbolRow) -> int:
        return next(ids)

    repo.upsert = fake_upsert  # type: ignore[method-assign]
    out = await repo.ensure_seeded(rows)
    assert out == {"BTC": 1, "ETH": 2}


async def test_candle_repository_insert_batch_and_get_recent() -> None:
    pool = FakePool()
    repo = CandleRepository(pool)

    rows = [
        CandleRow(
            symbol_id=1,
            ts=_TS,
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
            quote_volume=15.0,
            trades=5,
            taker_buy_volume=4.0,
        )
    ]
    count = await repo.insert_batch(rows)
    assert count == 1
    query, executed = pool.executemany_calls[0]
    assert "INSERT INTO candles" in query
    assert "ON CONFLICT (symbol_id, interval, ts) DO UPDATE" in query
    assert executed[0][0] == 1
    assert executed[0][2] == "1m"

    assert await repo.insert_batch([]) == 0
    assert pool.executemany_calls == [pool.executemany_calls[0]]  # not called again

    pool.fetch_return = [{"symbol_id": 1}]
    rows_out = await repo.get_recent(1, limit=10)
    assert rows_out == [{"symbol_id": 1}]
    query, args = pool.fetch_calls[0]
    assert "FROM candles" in query
    assert args == (1, "1m", 10)


async def test_indicator_repository_upsert_and_get_latest() -> None:
    pool = FakePool()
    repo = IndicatorRepository(pool)

    await repo.upsert(IndicatorRow(symbol_id=1, ts=_TS, ema_fast=1.0, rsi=50.0))
    query, args = pool.execute_calls[0]
    assert "INSERT INTO indicators" in query
    assert "ON CONFLICT (symbol_id, ts) DO UPDATE" in query
    assert args[0] == 1

    pool.fetchrow_return = {"symbol_id": 1, "rsi": 50.0}
    assert await repo.get_latest(1) == {"symbol_id": 1, "rsi": 50.0}


async def test_signal_repository_insert_and_get_recent() -> None:
    pool = FakePool()
    repo = SignalRepository(pool)

    pool.fetchrow_return = {"id": 99}
    signal_id = await repo.insert(SignalRow(symbol_id=1, ts=_TS, type="buy", confidence=80))
    assert signal_id == 99
    assert "INSERT INTO signals" in pool.fetchrow_calls[0][0]

    pool.fetch_return = [{"id": 99}]
    assert await repo.get_recent() == [{"id": 99}]
    query, args = pool.fetch_calls[0]
    assert "WHERE symbol_id" not in query
    assert args == (50,)

    await repo.get_recent(symbol_id=1, limit=5)
    query, args = pool.fetch_calls[1]
    assert "WHERE symbol_id = $1" in query
    assert args == (1, 5)


async def test_market_event_repository_insert_and_get_recent() -> None:
    pool = FakePool()
    repo = MarketEventRepository(pool)

    pool.fetchrow_return = {"id": 5}
    event_id = await repo.insert(MarketEventRow(symbol_id=1, ts=_TS, type="pump"))
    assert event_id == 5
    assert "INSERT INTO market_events" in pool.fetchrow_calls[0][0]

    pool.fetch_return = [{"id": 5}]
    assert await repo.get_recent(symbol_id=1) == [{"id": 5}]


async def test_market_memory_repository_insert_and_get_recent() -> None:
    pool = FakePool()
    repo = MarketMemoryRepository(pool)

    pool.fetchrow_return = {"id": 3}
    memory_id = await repo.insert_snapshot(MarketMemoryRow(ts=_TS, regime="bull"))
    assert memory_id == 3
    assert "INSERT INTO market_memory" in pool.fetchrow_calls[0][0]

    pool.fetch_return = [{"id": 3}]
    assert await repo.get_recent() == [{"id": 3}]


async def test_market_report_repository_insert_and_get_latest() -> None:
    pool = FakePool()
    repo = MarketReportRepository(pool)

    pool.fetchrow_return = {"id": 1}
    report_id = await repo.insert(MarketReportRow(ts=_TS, headline="Market update"))
    assert report_id == 1
    assert "INSERT INTO market_reports" in pool.fetchrow_calls[0][0]

    assert await repo.get_latest() == {"id": 1}


async def test_telegram_alert_repository_insert_and_dedupe() -> None:
    pool = FakePool()
    repo = TelegramAlertRepository(pool)

    pool.fetchrow_return = {"id": 1}
    alert_id = await repo.insert(
        TelegramAlertRow(ref_type="signal", ref_id=99, ts=_TS, status="sent", dedupe_key="k1")
    )
    assert alert_id == 1
    assert "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING" in (
        pool.fetchrow_calls[0][0]
    )

    pool.fetchrow_return = None  # conflict -> ON CONFLICT DO NOTHING -> no row
    alert_id = await repo.insert(
        TelegramAlertRow(ref_type="signal", ref_id=99, ts=_TS, status="sent", dedupe_key="k1")
    )
    assert alert_id == -1

    pool.fetch_return = [{"id": 1}]
    assert await repo.get_recent() == [{"id": 1}]
