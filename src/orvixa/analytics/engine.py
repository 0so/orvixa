"""Milestone 4 — orchestrates the deterministic analytics pipeline.

Wired the same way M2's ``CandleSink`` and M3's ``SymbolManager`` are wired
into their runners (see :mod:`orvixa.runners.analytics`):
:meth:`AnalyticsEngine.handle_candle` is registered via
``feed.on_candle_close`` and :meth:`handle_snapshot` via
``feed.on_market_snapshot``.

Per closed candle (one symbol):

1. fold the candle into the symbol's incremental indicators
   (:mod:`orvixa.analytics.indicators`);
2. compute trend direction/strength/slope (:mod:`orvixa.analytics.trend`);
3. queue an :class:`~orvixa.db.models.IndicatorRow` for batched
   ``IndicatorRepository.upsert_batch``;
4. evaluate signals (:mod:`orvixa.analytics.signals`) and persist any state
   transitions via ``SignalRepository``;
5. evaluate events (:mod:`orvixa.analytics.events`) and persist any via
   ``MarketEventRepository``.

Periodically (``regime_refresh_interval_seconds``), :meth:`refresh_regime`
combines the latest breadth snapshot (fed by :meth:`handle_snapshot` into
the M3 :class:`~orvixa.symbols.breadth.BreadthEngine`) with cross-symbol
trend participation to compute regime + health
(:mod:`orvixa.analytics.regime`/:mod:`orvixa.analytics.health`), persisted
via ``MarketMemoryRepository``.

All state is in-memory, per symbol, updated incrementally — no full-history
recalculation (M4 requirement #7), and indicator writes are batched so a
universe of hundreds of symbols doesn't cost hundreds of round trips per
candle close.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from ..config import Settings
from ..db.models import IndicatorRow, MarketEventRow, MarketMemoryRow, SignalRow
from ..db.repository import (
    IndicatorRepository,
    MarketEventRepository,
    MarketMemoryRepository,
    SignalRepository,
    SymbolRepository,
)
from ..feeds.base import Candle, TickerRow
from ..persistence.batch_writer import BatchWriter
from ..symbols.breadth import BreadthEngine
from ..symbols.models import BreadthSnapshot
from .events import EventEngine
from .indicators import SymbolIndicators
from .regime import RegimeEngine, RegimeResult
from .signals import SignalEngine
from .trend import TrendResult, compute_trend

logger = logging.getLogger("orvixa.analytics.engine")


def indicator_repository_sink(repo: IndicatorRepository):
    """Adapt :meth:`IndicatorRepository.upsert_batch` to the ``BatchWriter`` sink shape."""

    async def _sink(rows: list[IndicatorRow]) -> None:
        await repo.upsert_batch(rows)

    return _sink


class AnalyticsEngine:
    """Per-symbol incremental indicators/trend/signals/events + periodic regime/health."""

    def __init__(
        self,
        settings: Settings,
        symbol_repo: SymbolRepository,
        indicator_writer: BatchWriter[IndicatorRow],
        signal_repo: SignalRepository,
        event_repo: MarketEventRepository,
        memory_repo: MarketMemoryRepository,
        symbol_ids: dict[str, int] | None = None,
    ) -> None:
        self._settings = settings
        self._symbol_repo = symbol_repo
        self._indicator_writer = indicator_writer
        self._signal_repo = signal_repo
        self._event_repo = event_repo
        self._memory_repo = memory_repo

        self._symbol_ids: dict[str, int] = dict(symbol_ids or {})
        self._indicators: dict[int, SymbolIndicators] = {}
        self._latest_trend: dict[int, TrendResult] = {}

        self._signal_engine = SignalEngine(settings)
        self._event_engine = EventEngine(settings)
        self._regime_engine = RegimeEngine(settings)
        self._breadth = BreadthEngine(trend_window=settings.breadth_trend_window)
        self._latest_breadth: BreadthSnapshot | None = None

        self._task: asyncio.Task | None = None
        self._running = False

        # Observable state for tests/ops.
        self.candles_processed = 0
        self.signals_emitted = 0
        self.events_emitted = 0
        self.regime_refresh_count = 0

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="analytics-regime-loop")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._settings.regime_refresh_interval_seconds)
                if not self._running:
                    break
                await self.refresh_regime()
        except asyncio.CancelledError:
            raise

    # -- candle pipeline ------------------------------------------------------
    async def handle_candle(self, candle: Candle) -> None:
        """Callback for ``feed.on_candle_close``; ignores in-progress candles."""
        if not candle.closed:
            return

        symbol_id = await self._resolve_symbol_id(candle.symbol)
        if symbol_id is None:
            logger.warning("no symbols.id for %s; dropping candle", candle.symbol)
            return

        state = self._indicators.setdefault(symbol_id, SymbolIndicators(self._settings))
        snapshot = state.update(candle)
        trend = compute_trend(snapshot)
        if trend is not None:
            self._latest_trend[symbol_id] = trend
        else:
            self._latest_trend.pop(symbol_id, None)

        ts = datetime.fromtimestamp(candle.ts / 1000, tz=UTC)

        await self._indicator_writer.add(
            IndicatorRow(
                symbol_id=symbol_id,
                ts=ts,
                ema_fast=snapshot.ema_fast,
                ema_slow=snapshot.ema_slow,
                rsi=snapshot.rsi,
                atr=snapshot.atr,
                vol_realized=snapshot.vol_realized,
                vol_rel=snapshot.vol_rel,
                trend_score=trend.score if trend is not None else None,
            )
        )

        if self._settings.enable_signals:
            for signal in self._signal_engine.evaluate(symbol_id, snapshot, trend):
                await self._signal_repo.insert(
                    SignalRow(
                        symbol_id=symbol_id,
                        ts=ts,
                        type=signal.type,
                        confidence=signal.confidence,
                        components=signal.components,
                        state_from=signal.state_from,
                        state_to=signal.state_to,
                    )
                )
                self.signals_emitted += 1

        for event in self._event_engine.evaluate(symbol_id, snapshot):
            await self._event_repo.insert(
                MarketEventRow(
                    symbol_id=symbol_id,
                    ts=ts,
                    type=event.type,
                    magnitude=event.magnitude,
                    severity=event.severity,
                    price=event.price,
                    payload=event.payload,
                )
            )
            self.events_emitted += 1

        self.candles_processed += 1

    async def _resolve_symbol_id(self, base: str) -> int | None:
        symbol_id = self._symbol_ids.get(base)
        if symbol_id is not None:
            return symbol_id
        symbol_id = await self._symbol_repo.get_id(base)
        if symbol_id is not None:
            self._symbol_ids[base] = symbol_id
        return symbol_id

    # -- breadth (real-time, from the feed's snapshot stream) ----------------
    async def handle_snapshot(self, rows: list[TickerRow]) -> None:
        self._latest_breadth = self._breadth.update(rows)

    def get_breadth(self) -> BreadthSnapshot | None:
        return self._latest_breadth

    # -- regime / health -------------------------------------------------------
    async def refresh_regime(self) -> RegimeResult | None:
        """Combine the latest breadth + trend participation into a regime/health snapshot.

        Returns ``None`` (and persists nothing) until at least one breadth
        snapshot and one symbol's trend have been observed.
        """
        if self._latest_breadth is None or not self._latest_trend:
            return None

        trends = list(self._latest_trend.values())
        up = sum(1 for t in trends if t.direction == "up")
        down = sum(1 for t in trends if t.direction == "down")
        total = len(trends)
        up_frac = up / total
        down_frac = down / total

        vols = [
            state.vol_realized.value
            for state in list(self._indicators.values())
            if state.vol_realized.value is not None
        ]
        avg_vol = sum(vols) / len(vols) if vols else None

        result = self._regime_engine.evaluate(self._latest_breadth, up_frac, down_frac, avg_vol)

        await self._memory_repo.insert_snapshot(
            MarketMemoryRow(
                ts=datetime.now(tz=UTC),
                regime=result.regime,
                vol_regime=result.vol_regime,
                breadth=result.breadth,
                health_score=result.health_score,
                snapshot=result.snapshot,
            )
        )
        self.regime_refresh_count += 1
        return result
