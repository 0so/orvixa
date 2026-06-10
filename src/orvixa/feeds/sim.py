"""Offline, deterministic market feed.

A faithful port of the approved Phase-1 simulator: geometric-Brownian-motion
price paths with occasional pump/dump bursts, aggregated into 1-minute candles.
It satisfies :class:`MarketFeed` exactly like :class:`BinanceFeed`, so the whole
pipeline runs with no network — used for development, CI, and as the live
fallback when Binance is unreachable.

Seeding the RNG (``seed=``) makes a run byte-for-byte reproducible, which is what
the contract tests rely on.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Iterable

from ..logging import get_logger
from .base import Candle, MarketFeed, TickerRow
from .normalize import normalize_symbol

logger = get_logger("orvixa.feeds.sim")

# Per-symbol seed prices and relative volatility (mirrors the prototype universe).
_SEED_UNIVERSE: dict[str, tuple[float, float, float]] = {
    # pair: (price, rel_vol, quote_vol_base)
    "BTCUSDT": (68420.0, 1.00, 9200.0),
    "ETHUSDT": (3560.0, 1.15, 6100.0),
    "SOLUSDT": (168.4, 1.70, 4200.0),
    "BNBUSDT": (604.2, 1.05, 2100.0),
    "XRPUSDT": (0.612, 1.40, 1900.0),
    "AVAXUSDT": (38.6, 1.80, 1300.0),
    "LINKUSDT": (17.8, 1.60, 1150.0),
    "DOGEUSDT": (0.1632, 2.30, 3200.0),
    "1000PEPEUSDT": (0.01284, 3.20, 2900.0),
    "1000SHIBUSDT": (0.02618, 2.60, 2400.0),
    "WIFUSDT": (2.84, 3.60, 1450.0),
    "BONKUSDT": (0.03142, 3.80, 1100.0),
}
_DEFAULT_PRICE, _DEFAULT_VOL, _DEFAULT_QV = 1.0, 1.5, 800.0
_TICKS_PER_CANDLE = 7


class _SymbolState:
    __slots__ = ("pair", "price", "sigma", "qv_base", "mu", "burst", "burst_dir")

    def __init__(self, pair: str, rng: random.Random) -> None:
        price, vol, qv = _SEED_UNIVERSE.get(pair, (_DEFAULT_PRICE, _DEFAULT_VOL, _DEFAULT_QV))
        self.pair = pair
        self.price = price
        self.sigma = 0.0007 * vol
        self.qv_base = qv
        self.mu = (rng.random() - 0.5) * 0.00025
        self.burst = 0
        self.burst_dir = 0


class SimFeed(MarketFeed):
    """Deterministic simulator implementing the :class:`MarketFeed` contract."""

    def __init__(
        self,
        symbols: Iterable[str],
        candle_seconds: float = 3.0,
        seed: int | None = None,
    ) -> None:
        super().__init__()
        self._rng = random.Random(seed)
        self._candle_seconds = max(0.05, candle_seconds)
        self._states: dict[str, _SymbolState] = {
            s.upper(): _SymbolState(s.upper(), self._rng) for s in symbols
        }
        self._task: asyncio.Task | None = None
        self._running = False

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        logger.info("sim feed starting", extra={"symbols": list(self._states)})
        self._task = asyncio.create_task(self._loop(), name="sim-feed-loop")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("sim feed stopped")

    async def subscribe(self, symbols: Iterable[str]) -> None:
        for s in symbols:
            key = s.upper()
            if key not in self._states:
                self._states[key] = _SymbolState(key, self._rng)
                logger.info("sim subscribe", extra={"symbol": normalize_symbol(key)})

    async def unsubscribe(self, symbols: Iterable[str]) -> None:
        for s in symbols:
            self._states.pop(s.upper(), None)

    # -- internals ------------------------------------------------------
    def _step_price(self, st: _SymbolState) -> float:
        """Advance one symbol by a single tick; returns the tick's quote volume."""
        if self._rng.random() < 0.012:
            st.mu = (self._rng.random() - 0.5) * 0.0003
        if st.burst <= 0 and self._rng.random() < 0.0025:
            st.burst = 3 + int(self._rng.random() * 5)
            st.burst_dir = 1 if self._rng.random() < 0.5 else -1
        drift = st.mu
        shock = self._gauss() * st.sigma
        if st.burst > 0:
            drift += st.burst_dir * st.sigma * 3
            st.burst -= 1
        st.price = max(st.price * (1.0 + drift + shock), 1e-12)
        return st.qv_base * (0.6 + self._rng.random() * 0.9) * (1.0 + abs(shock) * 120.0)

    def _gauss(self) -> float:
        u = max(self._rng.random(), 1e-12)
        v = self._rng.random()
        return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)

    def _build_candle(self, st: _SymbolState) -> Candle:
        """Run one candle's worth of ticks and assemble the 1m bar."""
        open_p = st.price
        high = low = st.price
        quote_v = 0.0
        for _ in range(_TICKS_PER_CANDLE):
            quote_v += self._step_price(st)
            high = max(high, st.price)
            low = min(low, st.price)
        close = st.price
        volume = quote_v / close if close else 0.0
        # Approximate the taker-buy share of volume from the candle's
        # direction: a bullish bar implies more aggressive buying.
        buy_share = 0.5 + max(-0.4, min(0.4, (close - open_p) / open_p * 50.0)) if open_p else 0.5
        return Candle(
            symbol=normalize_symbol(st.pair),
            ts=int(time.time() * 1000),
            open=open_p,
            high=high,
            low=low,
            close=close,
            volume=volume,
            quote_volume=quote_v,
            trades=_TICKS_PER_CANDLE,
            closed=True,
            taker_buy_volume=volume * buy_share,
        )

    async def _loop(self) -> None:
        try:
            while self._running:
                rows: list[TickerRow] = []
                for st in list(self._states.values()):
                    candle = self._build_candle(st)
                    await self._emit_candle(candle)
                    rows.append(
                        TickerRow(
                            symbol=candle.symbol,
                            price=candle.close,
                            change_pct=(candle.close - candle.open) / candle.open * 100.0
                            if candle.open
                            else 0.0,
                            quote_volume=candle.quote_volume,
                        )
                    )
                await self._emit_snapshot(rows)
                await asyncio.sleep(self._candle_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("sim feed loop crashed")
            raise
