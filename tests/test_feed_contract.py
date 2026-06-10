"""Contract test: SimFeed and BinanceFeed must honor the same MarketFeed shape.

Both sources are exercised through the public interface only. BinanceFeed runs
against a fake in-memory socket so the suite stays offline and deterministic.
"""

from __future__ import annotations

import asyncio
import json

from orvixa.feeds.base import Candle, MarketFeed, TickerRow
from orvixa.feeds.binance import BinanceFeed
from orvixa.feeds.sim import SimFeed


class _Collector:
    def __init__(self) -> None:
        self.candles: list[Candle] = []
        self.snapshots: list[list[TickerRow]] = []

    async def on_candle(self, c: Candle) -> None:
        self.candles.append(c)

    async def on_snapshot(self, rows: list[TickerRow]) -> None:
        self.snapshots.append(rows)


def _assert_valid_candle(c: Candle) -> None:
    assert isinstance(c, Candle)
    assert isinstance(c.symbol, str) and c.symbol
    assert c.symbol == c.symbol.upper()
    assert isinstance(c.ts, int) and c.ts > 0
    assert c.high >= c.low
    assert c.high >= c.open and c.high >= c.close
    assert c.low <= c.open and c.low <= c.close
    assert c.volume >= 0 and c.quote_volume >= 0
    assert c.taker_buy_volume >= 0
    assert isinstance(c.closed, bool)


def _assert_valid_rows(rows: list[TickerRow]) -> None:
    assert isinstance(rows, list) and rows
    for r in rows:
        assert isinstance(r, TickerRow)
        assert r.symbol == r.symbol.upper()
        assert r.price > 0


# --------------------------------------------------------------------------- #
# Fake websocket for BinanceFeed
# --------------------------------------------------------------------------- #
class _FakeWS:
    """Async-iterable fake socket that yields a fixed list of frames, then idles."""

    def __init__(self, frames: list[str]) -> None:
        self._frames = frames
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for frame in self._frames:
            yield frame
        # keep the connection "open" without busy-looping
        while not self.closed:
            await asyncio.sleep(0.01)


def _kline_frame(symbol: str, close: float, is_closed: bool = True) -> str:
    return json.dumps(
        {
            "stream": f"{symbol.lower()}@kline_1m",
            "data": {
                "e": "kline", "E": 1, "s": symbol,
                "k": {
                    "t": 1718030400000, "T": 1718030459999, "s": symbol, "i": "1m",
                    "o": "100.0", "c": str(close), "h": str(close + 1), "l": "99.0",
                    "v": "10", "n": 5, "x": is_closed, "q": "1000",
                },
            },
        }
    )


def _miniticker_frame() -> str:
    return json.dumps(
        {
            "stream": "!miniTicker@arr",
            "data": [
                {"s": "BTCUSDT", "c": "69000", "o": "68000", "q": "1000"},
                {"s": "ETHUSDT", "c": "3500", "o": "3550", "q": "800"},
            ],
        }
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_simfeed_satisfies_contract() -> None:
    coll = _Collector()
    feed = SimFeed(["BTCUSDT", "ETHUSDT", "1000PEPEUSDT"], candle_seconds=0.05, seed=42)
    assert isinstance(feed, MarketFeed)
    feed.on_candle_close(coll.on_candle)
    feed.on_market_snapshot(coll.on_snapshot)

    await feed.start()
    await asyncio.sleep(0.18)
    await feed.stop()

    assert coll.candles, "sim feed produced no candles"
    assert coll.snapshots, "sim feed produced no snapshots"
    for c in coll.candles:
        _assert_valid_candle(c)
    for rows in coll.snapshots:
        _assert_valid_rows(rows)
    # PEPE pair must be normalized to its display symbol
    assert {"BTC", "ETH", "PEPE"} <= {c.symbol for c in coll.candles}


async def test_simfeed_is_deterministic_with_seed() -> None:
    # White-box determinism: identical seeds must yield identical candle paths,
    # independent of wall-clock scheduling. Drive _build_candle directly.
    f1 = SimFeed(["BTCUSDT"], seed=7)
    f2 = SimFeed(["BTCUSDT"], seed=7)
    st1 = f1._states["BTCUSDT"]
    st2 = f2._states["BTCUSDT"]
    closes1 = [f1._build_candle(st1).close for _ in range(5)]
    closes2 = [f2._build_candle(st2).close for _ in range(5)]
    assert closes1 == closes2

    # A different seed must (almost surely) diverge.
    f3 = SimFeed(["BTCUSDT"], seed=99)
    closes3 = [f3._build_candle(f3._states["BTCUSDT"]).close for _ in range(5)]
    assert closes3 != closes1


async def test_binancefeed_satisfies_contract() -> None:
    coll = _Collector()
    frames = [_kline_frame("BTCUSDT", 69000.0), _miniticker_frame()]

    fake = _FakeWS(frames)

    async def connector(url: str):
        assert "btcusdt@kline_1m" in url
        assert "!miniTicker@arr" in url
        return fake

    async def backfiller(symbols, limit):
        return []  # no gap-fill noise in this test

    feed = BinanceFeed(["BTCUSDT"], connector=connector, backfiller=backfiller)
    assert isinstance(feed, MarketFeed)
    feed.on_candle_close(coll.on_candle)
    feed.on_market_snapshot(coll.on_snapshot)

    await feed.start()
    assert await feed.wait_connected(timeout=1.0)
    await asyncio.sleep(0.1)
    await feed.stop()

    assert coll.candles, "binance feed produced no candles"
    assert coll.snapshots, "binance feed produced no snapshots"
    _assert_valid_candle(coll.candles[0])
    assert coll.candles[0].symbol == "BTC"
    _assert_valid_rows(coll.snapshots[0])


async def test_binancefeed_subscribe_sends_frame() -> None:
    fake = _FakeWS([])

    async def connector(url: str):
        return fake

    async def backfiller(symbols, limit):
        return []

    feed = BinanceFeed(["BTCUSDT"], connector=connector, backfiller=backfiller)
    await feed.start()
    assert await feed.wait_connected(timeout=1.0)
    await feed.subscribe(["ETHUSDT"])
    await asyncio.sleep(0.02)
    await feed.stop()

    assert any("SUBSCRIBE" in m and "ethusdt@kline_1m" in m for m in fake.sent)
