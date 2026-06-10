"""The market-feed contract — the one seam every source plugs into.

``SimFeed`` and ``BinanceFeed`` both subclass ``MarketFeed``. Everything
downstream (persistence, indicators, the API) depends only on this module and
the two dataclasses it defines — never on a concrete source or an exchange
payload shape.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

logger = logging.getLogger("orvixa.feeds")

CandleCallback = Callable[["Candle"], Awaitable[None]]
SnapshotCallback = Callable[[list["TickerRow"]], Awaitable[None]]


@dataclass(slots=True)
class Candle:
    """A 1-minute OHLCV bar, normalized across sources.

    ``ts`` is the candle *open* time in epoch milliseconds (UTC). ``closed`` is
    ``True`` only on the final update of the minute — consumers that want
    finalized data should gate on it.
    """

    symbol: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    closed: bool
    # Taker-buy base-asset volume — maps to candles.taker_buy_v in the M2
    # schema. Optional/additive so existing positional construction (M1
    # tests, fixtures) keeps working unchanged.
    taker_buy_volume: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


@dataclass(slots=True)
class TickerRow:
    """One row of a whole-market snapshot (from the ticker-array stream)."""

    symbol: str
    price: float
    change_pct: float
    quote_volume: float


class MarketFeed(ABC):
    """Source-agnostic market feed.

    Lifecycle (``start``/``stop``/``subscribe``/``unsubscribe``) is abstract.
    Callback registration and fan-out are concrete and shared by every source.
    """

    def __init__(self) -> None:
        self._candle_cbs: list[CandleCallback] = []
        self._snapshot_cbs: list[SnapshotCallback] = []

    # -- consumer registration ------------------------------------------
    def on_candle_close(self, cb: CandleCallback) -> None:
        """Register a coroutine called for every candle update (check ``closed``)."""
        self._candle_cbs.append(cb)

    def on_market_snapshot(self, cb: SnapshotCallback) -> None:
        """Register a coroutine called for each whole-market snapshot."""
        self._snapshot_cbs.append(cb)

    # -- emit helpers (used by subclasses) ------------------------------
    async def _emit_candle(self, candle: Candle) -> None:
        await self._fan_out(self._candle_cbs, candle)

    async def _emit_snapshot(self, rows: list[TickerRow]) -> None:
        await self._fan_out(self._snapshot_cbs, rows)

    @staticmethod
    async def _fan_out(callbacks: list, payload: object) -> None:
        # A misbehaving consumer must never take down the feed.
        for cb in callbacks:
            try:
                await cb(payload)
            except Exception:  # noqa: BLE001 - intentional isolation
                logger.exception("feed callback raised")

    # -- lifecycle (implemented per source) -----------------------------
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def subscribe(self, symbols: Iterable[str]) -> None: ...

    @abstractmethod
    async def unsubscribe(self, symbols: Iterable[str]) -> None: ...
