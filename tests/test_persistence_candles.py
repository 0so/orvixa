"""``CandleSink`` and symbol-registry seeding — no database required."""

from __future__ import annotations

import asyncio

from fake_pool import FakePool
from orvixa.config import Settings
from orvixa.db.models import CandleRow
from orvixa.db.repository import CandleRepository, SymbolRepository
from orvixa.feeds.base import Candle
from orvixa.persistence.batch_writer import BatchWriter
from orvixa.persistence.candles import CandleSink, candle_repository_sink
from orvixa.persistence.registry import build_symbol_rows


def _candle(symbol: str = "BTC", closed: bool = True) -> Candle:
    return Candle(
        symbol=symbol,
        ts=1_700_000_000_000,
        open=100.0,
        high=110.0,
        low=90.0,
        close=105.0,
        volume=10.0,
        quote_volume=1_000.0,
        trades=42,
        closed=closed,
        taker_buy_volume=6.0,
    )


class _RecordingWriter:
    def __init__(self) -> None:
        self.added: list[CandleRow] = []

    async def add(self, item: CandleRow) -> None:
        self.added.append(item)


async def test_candle_sink_ignores_unclosed_candles() -> None:
    pool = FakePool()
    repo = SymbolRepository(pool)
    writer = _RecordingWriter()
    sink = CandleSink(repo, writer, symbol_ids={"BTC": 1})  # type: ignore[arg-type]

    await sink.handle_candle(_candle(closed=False))

    assert writer.added == []


async def test_candle_sink_resolves_symbol_from_cache() -> None:
    pool = FakePool()
    repo = SymbolRepository(pool)
    writer = _RecordingWriter()
    sink = CandleSink(repo, writer, symbol_ids={"BTC": 7})  # type: ignore[arg-type]

    await sink.handle_candle(_candle())

    assert len(writer.added) == 1
    row = writer.added[0]
    assert row.symbol_id == 7
    assert row.open == 100.0
    assert row.taker_buy_volume == 6.0
    assert row.interval == "1m"
    assert pool.fetchval_calls == []  # cache hit, no DB lookup


async def test_candle_sink_resolves_symbol_via_repository_and_caches() -> None:
    pool = FakePool()
    pool.fetchval_return = 9
    repo = SymbolRepository(pool)
    writer = _RecordingWriter()
    sink = CandleSink(repo, writer)  # type: ignore[arg-type]

    await sink.handle_candle(_candle(symbol="ETH"))
    await sink.handle_candle(_candle(symbol="ETH"))

    assert len(writer.added) == 2
    assert all(row.symbol_id == 9 for row in writer.added)
    assert len(pool.fetchval_calls) == 1  # second lookup served from cache


async def test_candle_sink_drops_candle_for_unknown_symbol() -> None:
    pool = FakePool()
    pool.fetchval_return = None
    repo = SymbolRepository(pool)
    writer = _RecordingWriter()
    sink = CandleSink(repo, writer)  # type: ignore[arg-type]

    await sink.handle_candle(_candle(symbol="UNKNOWN"))

    assert writer.added == []


async def test_candle_repository_sink_calls_insert_batch() -> None:
    pool = FakePool()
    repo = CandleRepository(pool)
    sink = candle_repository_sink(repo)

    rows = [
        CandleRow(
            symbol_id=1,
            ts=_candle().ts,  # type: ignore[arg-type]
            open=1,
            high=2,
            low=0,
            close=1,
            volume=1,
            quote_volume=1,
            trades=1,
            taker_buy_volume=1,
        )
    ]
    # the sink should accept whatever insert_batch accepts; here just check
    # it doesn't raise against the FakePool.
    await sink(rows)
    assert pool.executemany_calls


async def test_batch_writer_with_candle_sink_end_to_end() -> None:
    pool = FakePool()
    pool.fetchval_return = 1
    repo = SymbolRepository(pool)

    candle_repo = CandleRepository(pool)
    writer: BatchWriter[CandleRow] = BatchWriter(
        candle_repository_sink(candle_repo), max_size=1, interval_seconds=10.0
    )
    sink = CandleSink(repo, writer, symbol_ids={"BTC": 1})

    await writer.start()
    try:
        await sink.handle_candle(_candle())
        for _ in range(50):
            if pool.executemany_calls:
                break
            await asyncio.sleep(0.01)
    finally:
        await writer.stop()

    assert pool.executemany_calls


def test_build_symbol_rows_classification() -> None:
    settings = Settings(
        core_symbols="BTCUSDT,ETHUSDT",
        seed_symbols="DOGEUSDT,1000PEPEUSDT,LINKUSDT",
    )

    rows = build_symbol_rows(settings)
    by_base = {row.base: row for row in rows}

    assert by_base["BTC"].klass == "core"
    assert by_base["BTC"].tier == 0
    assert by_base["DOGE"].klass == "meme"
    assert by_base["PEPE"].klass == "meme"
    assert by_base["LINK"].klass == "alt"
    assert by_base["LINK"].tier == 1
    assert by_base["BTC"].symbol == "BTCUSDT"
