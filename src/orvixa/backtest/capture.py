"""In-memory sinks standing in for the M4 repositories during replay.

:class:`~orvixa.analytics.engine.AnalyticsEngine` is constructed and run
unmodified, but the signal-validation harness has no Postgres writes: the
indicator/event/regime outputs aren't part of this question, and signals are
captured in memory rather than persisted. ``NullSink`` discards everything;
``SignalCaptureSink`` keeps every emitted :class:`SignalRow`, in order.
"""

from __future__ import annotations

from ..db.models import IndicatorRow, MarketEventRow, MarketMemoryRow, SignalRow


class NullSink:
    """Discards rows -- used for the indicator/event/memory engine args."""

    async def add(self, item: IndicatorRow) -> None:
        pass

    async def insert(self, item: MarketEventRow) -> None:
        pass

    async def insert_snapshot(self, item: MarketMemoryRow) -> None:
        pass


class SignalCaptureSink:
    """Captures every :class:`SignalRow` the SignalEngine emits, in order."""

    def __init__(self) -> None:
        self.rows: list[SignalRow] = []

    async def insert(self, item: SignalRow) -> None:
        self.rows.append(item)
