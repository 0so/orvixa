"""Market breadth engine.

Consumes the same whole-market ``!miniTicker@arr`` snapshots M1 already emits
via ``feed.on_market_snapshot`` (see :class:`~orvixa.feeds.base.TickerRow`)
and derives market-wide health metrics: advancers/decliners,
advance/decline ratio, percentage of symbols trading above their short-term
trend, and new highs/lows.

A single snapshot only carries last price + 24h change, so "trend" and
"highs/lows" are derived from a rolling per-symbol price window maintained
across calls to :meth:`BreadthEngine.update`.
"""

from __future__ import annotations

from collections import deque

from ..feeds.base import TickerRow
from .models import BreadthSnapshot

_MIN_TREND_WINDOW = 2


class BreadthEngine:
    """Stateful market-breadth calculator fed by successive ticker snapshots."""

    def __init__(self, trend_window: int = 20) -> None:
        self._trend_window = max(_MIN_TREND_WINDOW, trend_window)
        self._history: dict[str, deque[float]] = {}

    def update(self, rows: list[TickerRow]) -> BreadthSnapshot:
        """Fold one whole-market snapshot into the rolling state and return breadth."""
        advancers = decliners = unchanged = 0
        above_trend = 0
        new_highs = new_lows = 0
        total = len(rows)

        for row in rows:
            if row.change_pct > 0:
                advancers += 1
            elif row.change_pct < 0:
                decliners += 1
            else:
                unchanged += 1

            history = self._history.setdefault(row.symbol, deque(maxlen=self._trend_window))
            if history:
                trend = sum(history) / len(history)
                if row.price > trend:
                    above_trend += 1
                if row.price > max(history):
                    new_highs += 1
                if row.price < min(history):
                    new_lows += 1
            history.append(row.price)

        ad_ratio = (advancers / decliners) if decliners else float(advancers)
        pct_above_trend = (above_trend / total * 100.0) if total else 0.0

        return BreadthSnapshot(
            total=total,
            advancers=advancers,
            decliners=decliners,
            unchanged=unchanged,
            ad_ratio=ad_ratio,
            pct_above_trend=pct_above_trend,
            new_highs=new_highs,
            new_lows=new_lows,
        )
