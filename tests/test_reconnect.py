"""Resilience: BinanceFeed must back off, reconnect, resubscribe, and gap-fill.

Drives the full reconnect path with an injected connector that fails a set
number of times before yielding a working socket — no network, fully
deterministic.
"""

from __future__ import annotations

import asyncio

from orvixa.feeds.base import Candle
from orvixa.feeds.binance import BinanceFeed


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while not self.closed:
            await asyncio.sleep(0.01)
        return
        yield  # pragma: no cover - makes this an async generator


async def test_backoff_then_reconnect(monkeypatch) -> None:
    # make backoff instant so the test is fast, but still observable
    monkeypatch.setattr(BinanceFeed, "_next_backoff", lambda self, attempt: 0.01)

    attempts = {"n": 0}
    good = _FakeWS()

    async def flaky_connector(url: str):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise ConnectionError("simulated drop")
        return good

    backfills = {"n": 0}

    async def backfiller(symbols, limit) -> list[Candle]:
        backfills["n"] += 1
        return []

    feed = BinanceFeed(
        ["BTCUSDT", "ETHUSDT"],
        connector=flaky_connector,
        backfiller=backfiller,
        max_reconnects=5,
    )
    await feed.start()
    connected = await feed.wait_connected(timeout=2.0)
    await asyncio.sleep(0.05)
    await feed.stop()

    assert connected, "feed never reached a connected state"
    # two failures → two backoff entries recorded
    assert len(feed.backoff_history) == 2
    # connect was attempted three times total (2 fail + 1 success)
    assert feed.connect_count == 3
    # gap-fill ran on the successful connect
    assert backfills["n"] >= 1
    assert feed.gapfill_count >= 1


async def test_resubscribe_after_reconnect(monkeypatch) -> None:
    monkeypatch.setattr(BinanceFeed, "_next_backoff", lambda self, attempt: 0.01)

    sockets: list[_FakeWS] = []
    state = {"first": True}

    async def connector(url: str):
        ws = _FakeWS()
        sockets.append(ws)
        # drop the first socket shortly after connecting to force a reconnect
        if state["first"]:
            state["first"] = False

            async def _kill() -> None:
                await asyncio.sleep(0.05)
                ws.closed = True

            asyncio.create_task(_kill())
        return ws

    async def backfiller(symbols, limit):
        return []

    feed = BinanceFeed(["BTCUSDT"], connector=connector, backfiller=backfiller, max_reconnects=5)
    await feed.start()
    await feed.wait_connected(timeout=2.0)
    await asyncio.sleep(0.2)  # allow the kill + reconnect cycle
    await feed.stop()

    # at least two sockets were opened (initial + reconnect)
    assert len(sockets) >= 2
    assert feed.resubscribe_count >= 1


async def test_gap_fill_emits_candles(monkeypatch) -> None:
    fake = _FakeWS()

    async def connector(url: str):
        return fake

    async def backfiller(symbols, limit):
        return [
            Candle("BTC", 1718030400000, 1, 2, 0.5, 1.5, 10, 15, 5, True),
            Candle("ETH", 1718030400000, 1, 2, 0.5, 1.5, 10, 15, 5, True),
        ]

    received: list[Candle] = []

    async def on_candle(c: Candle) -> None:
        received.append(c)

    feed = BinanceFeed(["BTCUSDT", "ETHUSDT"], connector=connector, backfiller=backfiller)
    feed.on_candle_close(on_candle)
    await feed.start()
    await feed.wait_connected(timeout=1.0)
    await asyncio.sleep(0.05)
    await feed.stop()

    assert {c.symbol for c in received} == {"BTC", "ETH"}
