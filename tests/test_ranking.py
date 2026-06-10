"""Tests for :mod:`orvixa.symbols.ranking`."""

from __future__ import annotations

from orvixa.symbols.models import TickerStats
from orvixa.symbols.ranking import compute_score, rank_by_score


def _stats(pair: str, quote_volume: float, change_pct: float = 0.0, count: int = 0) -> TickerStats:
    return TickerStats(
        pair=pair, last_price=1.0, quote_volume=quote_volume, price_change_pct=change_pct, count=count
    )


def test_higher_volume_scores_higher() -> None:
    low = _stats("LOWUSDT", 1_000.0)
    high = _stats("HIGHUSDT", 100_000.0)
    assert compute_score(high) > compute_score(low)


def test_activity_and_volatility_break_ties_among_equal_volume() -> None:
    plain = _stats("PLAINUSDT", 100_000.0)
    active = _stats("ACTIVEUSDT", 100_000.0, count=200_000)
    volatile = _stats("VOLATILEUSDT", 100_000.0, change_pct=-15.0)

    assert compute_score(active) > compute_score(plain)
    assert compute_score(volatile) > compute_score(plain)


def test_rank_by_score_orders_descending() -> None:
    btc = _stats("BTCUSDT", 1_000_000.0, count=100_000)
    eth = _stats("ETHUSDT", 500_000.0, count=50_000)
    sol = _stats("SOLUSDT", 50_000.0, count=10_000)

    ranked = rank_by_score([sol, btc, eth])

    pairs = [s.pair for s, _score in ranked]
    assert pairs == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    scores = [score for _s, score in ranked]
    assert scores == sorted(scores, reverse=True)


def test_rank_by_score_handles_empty() -> None:
    assert rank_by_score([]) == []
