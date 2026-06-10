"""Tests for :func:`orvixa.analytics.health.compute_health_score` (M4)."""

from __future__ import annotations

from orvixa.analytics.health import compute_health_score
from orvixa.symbols.models import BreadthSnapshot


def _breadth(ad_ratio: float, pct_above_trend: float) -> BreadthSnapshot:
    return BreadthSnapshot(
        total=10,
        advancers=7,
        decliners=3,
        unchanged=0,
        ad_ratio=ad_ratio,
        pct_above_trend=pct_above_trend,
        new_highs=1,
        new_lows=0,
    )


def test_strong_breadth_and_trend_score_high() -> None:
    score = compute_health_score(_breadth(ad_ratio=2.0, pct_above_trend=100.0), trend_up_frac=1.0, vol_regime="normal")
    assert score == 100


def test_weak_breadth_and_trend_score_low() -> None:
    score = compute_health_score(_breadth(ad_ratio=0.0, pct_above_trend=0.0), trend_up_frac=0.0, vol_regime="normal")
    assert score == 0


def test_high_volatility_penalizes_score() -> None:
    normal = compute_health_score(_breadth(ad_ratio=1.0, pct_above_trend=50.0), trend_up_frac=0.5, vol_regime="normal")
    high = compute_health_score(_breadth(ad_ratio=1.0, pct_above_trend=50.0), trend_up_frac=0.5, vol_regime="high")
    assert high == normal - 15


def test_low_volatility_boosts_score() -> None:
    normal = compute_health_score(_breadth(ad_ratio=1.0, pct_above_trend=50.0), trend_up_frac=0.5, vol_regime="normal")
    low = compute_health_score(_breadth(ad_ratio=1.0, pct_above_trend=50.0), trend_up_frac=0.5, vol_regime="low")
    assert low == normal + 5


def test_score_bounded_to_0_100() -> None:
    score = compute_health_score(_breadth(ad_ratio=2.0, pct_above_trend=100.0), trend_up_frac=1.0, vol_regime="low")
    assert score == 100

    score_low = compute_health_score(_breadth(ad_ratio=0.0, pct_above_trend=0.0), trend_up_frac=0.0, vol_regime="high")
    assert score_low == 0
