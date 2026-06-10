"""Volume ranking engine.

Ranks the discovered USDT spot universe by a composite score combining 24h
quote volume (the primary liquidity signal) with light multipliers for
activity (trade count) and volatility (24h change), so very active or very
volatile symbols rank slightly ahead of otherwise-equal-volume peers.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import TickerStats

# Trade-count above this is treated as "fully active" for the activity factor.
_ACTIVITY_SATURATION = 100_000.0
# 24h change percentage above this is treated as "fully volatile".
_VOLATILITY_SATURATION = 100.0


def compute_score(stats: TickerStats) -> float:
    """Composite ranking score: quote volume scaled by activity/volatility."""
    activity_factor = 1.0 + min(stats.count / _ACTIVITY_SATURATION, 1.0) * 0.1
    volatility_factor = 1.0 + min(abs(stats.price_change_pct) / _VOLATILITY_SATURATION, 0.5)
    return stats.quote_volume * activity_factor * volatility_factor


def rank_by_score(stats: Iterable[TickerStats]) -> list[tuple[TickerStats, float]]:
    """Sort ``stats`` by descending :func:`compute_score`.

    Returns ``(stats, score)`` pairs; rank position is the list index + 1.
    """
    scored = [(s, compute_score(s)) for s in stats]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
