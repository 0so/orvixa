"""Integration tests for :class:`orvixa.analytics.engine.AnalyticsEngine` (M4).

Fully fake-driven — :class:`fake_pool.FakePool` stands in for Postgres (no
live database), and candles/ticker rows are constructed directly rather than
via a feed, mirroring the M1-M3 fake-driven test patterns.
"""

from __future__ import annotations

from fake_pool import FakePool
from orvixa.analytics.engine import AnalyticsEngine
from orvixa.config import Settings
from orvixa.db.models import IndicatorRow
from orvixa.db.repository import (
    MarketEventRepository,
    MarketMemoryRepository,
    SignalRepository,
    SymbolRepository,
)
from orvixa.feeds.base import Candle, TickerRow


class _FakeIndicatorWriter:
    def __init__(self) -> None:
        self.rows: list[IndicatorRow] = []

    async def add(self, item: IndicatorRow) -> None:
        self.rows.append(item)


def _candle(ts: int, o: float, h: float, low: float, c: float, v: float) -> Candle:
    return Candle(
        symbol="BTC",
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        quote_volume=v * c,
        trades=1,
        closed=True,
    )


def _settings(**overrides) -> Settings:
    defaults = {
        "ema_fast_period": 2,
        "ema_slow_period": 3,
        "rsi_period": 2,
        "atr_period": 2,
        "realized_vol_window": 2,
        "relative_volume_window": 2,
        "breakout_window": 2,
        "pump_dump_window": 2,
        "pump_dump_pct": 1.0,
        "vol_spike_window": 2,
        "vol_spike_multiplier": 1.0,
        "high_volatility_pct": 0.0001,
        "signal_min_confidence": 0,
        "regime_refresh_interval_seconds": 10_000.0,
        # Engine-level signal evaluation/persistence path is still covered by
        # tests even though it's disabled by default during the 30-day
        # Market Intelligence evaluation (Settings.enable_signals).
        "enable_signals": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_engine(settings: Settings | None = None):
    pool = FakePool()
    pool.fetchrow_return = {"id": 1}
    symbol_repo = SymbolRepository(pool)
    writer = _FakeIndicatorWriter()
    engine = AnalyticsEngine(
        settings or _settings(),
        symbol_repo,
        writer,
        SignalRepository(pool),
        MarketEventRepository(pool),
        MarketMemoryRepository(pool),
        symbol_ids={"BTC": 1},
    )
    return engine, pool, writer


_CANDLES = [
    _candle(0, 100, 101, 99, 100, 10),
    _candle(60_000, 100, 103, 99, 103, 12),
    _candle(120_000, 103, 108, 102, 108, 15),
    _candle(180_000, 108, 120, 107, 120, 50),
    _candle(240_000, 120, 130, 119, 128, 60),
]


async def test_handle_candle_ignores_unclosed() -> None:
    engine, _pool, writer = _make_engine()
    candle = _candle(0, 100, 101, 99, 100, 10)
    candle.closed = False

    await engine.handle_candle(candle)
    assert engine.candles_processed == 0
    assert writer.rows == []


async def test_handle_candle_queues_indicator_rows() -> None:
    engine, _pool, writer = _make_engine()

    for candle in _CANDLES:
        await engine.handle_candle(candle)

    assert engine.candles_processed == len(_CANDLES)
    assert len(writer.rows) == len(_CANDLES)

    first = writer.rows[0]
    assert first.symbol_id == 1
    assert first.ema_fast == 100.0
    assert first.rsi is None  # RSI needs > 1 candle

    last = writer.rows[-1]
    assert last.rsi is not None
    assert last.atr is not None
    assert last.vol_realized is not None
    assert last.vol_rel is not None
    assert last.trend_score is not None


async def test_handle_candle_unknown_symbol_dropped() -> None:
    engine, _pool, writer = _make_engine()
    candle = _candle(0, 100, 101, 99, 100, 10)
    candle.symbol = "UNKNOWN"

    await engine.handle_candle(candle)
    assert engine.candles_processed == 0
    assert writer.rows == []


async def test_signals_and_events_persisted() -> None:
    engine, pool, _writer = _make_engine()

    for candle in _CANDLES:
        await engine.handle_candle(candle)

    # HIGH VOLATILITY threshold is ~0 -> should trigger at least once.
    assert engine.signals_emitted >= 1
    signal_queries = [q for q, _ in pool.fetchrow_calls if "INSERT INTO signals" in q]
    assert signal_queries

    # pump_dump_pct=1.0 and breakout_window=2 with rising prices -> events.
    assert engine.events_emitted >= 1
    event_queries = [q for q, _ in pool.fetchrow_calls if "INSERT INTO market_events" in q]
    assert event_queries


async def test_handle_snapshot_updates_breadth() -> None:
    engine, _pool, _writer = _make_engine()
    assert engine.get_breadth() is None

    rows = [
        TickerRow(symbol="BTC", price=100.0, change_pct=1.0, quote_volume=1000.0),
        TickerRow(symbol="ETH", price=50.0, change_pct=-1.0, quote_volume=500.0),
    ]
    await engine.handle_snapshot(rows)

    breadth = engine.get_breadth()
    assert breadth is not None
    assert breadth.total == 2
    assert breadth.advancers == 1
    assert breadth.decliners == 1


async def test_refresh_regime_noop_without_data() -> None:
    engine, pool, _writer = _make_engine()
    result = await engine.refresh_regime()
    assert result is None
    assert not any("INSERT INTO market_memory" in q for q, _ in pool.fetchrow_calls)


async def test_refresh_regime_persists_market_memory() -> None:
    engine, pool, _writer = _make_engine()

    for candle in _CANDLES:
        await engine.handle_candle(candle)

    await engine.handle_snapshot(
        [
            TickerRow(symbol="BTC", price=128.0, change_pct=10.0, quote_volume=1000.0),
            TickerRow(symbol="ETH", price=50.0, change_pct=-1.0, quote_volume=500.0),
        ]
    )

    result = await engine.refresh_regime()
    assert result is not None
    assert result.regime in ("risk_on", "risk_off", "rotational")
    assert engine.regime_refresh_count == 1

    memory_queries = [q for q, _ in pool.fetchrow_calls if "INSERT INTO market_memory" in q]
    assert memory_queries
