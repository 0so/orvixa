"""Bridge from :class:`~orvixa.feeds.base.MarketFeed` candle closes to ``candles``.

``CandleSink.handle_candle`` is registered via ``feed.on_candle_close`` (see
:mod:`orvixa.runners.ingest`). It only acts on *closed* candles, resolves the
feed's display symbol (e.g. ``"BTC"``) to ``symbols.id`` via
:class:`~orvixa.db.repository.SymbolRepository` (cached after first lookup),
and hands the resulting :class:`~orvixa.db.models.CandleRow` to a
:class:`~orvixa.persistence.batch_writer.BatchWriter` for batched persistence.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..db.models import CandleRow
from ..db.repository import CandleRepository, SymbolRepository
from ..feeds.base import Candle
from .batch_writer import BatchWriter

logger = logging.getLogger("orvixa.persistence.candles")


def candle_repository_sink(repo: CandleRepository):
    """Adapt :meth:`CandleRepository.insert_batch` to the ``BatchWriter`` sink shape."""

    async def _sink(rows: list[CandleRow]) -> None:
        await repo.insert_batch(rows)

    return _sink


class CandleSink:
    """Turns closed feed candles into batched ``candles`` table writes."""

    def __init__(
        self,
        symbol_repo: SymbolRepository,
        batch_writer: BatchWriter[CandleRow],
        interval: str = "1m",
        symbol_ids: dict[str, int] | None = None,
    ) -> None:
        self._symbol_repo = symbol_repo
        self._batch_writer = batch_writer
        self._interval = interval
        self._symbol_ids: dict[str, int] = dict(symbol_ids or {})

    async def handle_candle(self, candle: Candle) -> None:
        """Callback for ``feed.on_candle_close``; ignores in-progress candles."""
        if not candle.closed:
            return
        symbol_id = await self._resolve_symbol_id(candle.symbol)
        if symbol_id is None:
            logger.warning("no symbols.id for %s; dropping candle", candle.symbol)
            return
        row = CandleRow(
            symbol_id=symbol_id,
            ts=datetime.fromtimestamp(candle.ts / 1000, tz=UTC),
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            quote_volume=candle.quote_volume,
            trades=candle.trades,
            taker_buy_volume=candle.taker_buy_volume,
            interval=self._interval,
        )
        await self._batch_writer.add(row)

    async def _resolve_symbol_id(self, base: str) -> int | None:
        symbol_id = self._symbol_ids.get(base)
        if symbol_id is not None:
            return symbol_id
        symbol_id = await self._symbol_repo.get_id(base)
        if symbol_id is not None:
            self._symbol_ids[base] = symbol_id
        return symbol_id
