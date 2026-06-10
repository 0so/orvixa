"""Tests for :func:`orvixa.analytics.trend.compute_trend` (M4)."""

from __future__ import annotations

from orvixa.analytics.indicators import IndicatorSnapshot
from orvixa.analytics.trend import compute_trend


def _snapshot(ema_fast, ema_slow, ema_slow_prev=None, **extra) -> IndicatorSnapshot:
    base = {
        "close": 100.0,
        "high": 101.0,
        "low": 99.0,
        "volume": 10.0,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_slow_prev": ema_slow_prev,
        "rsi": None,
        "atr": None,
        "vol_realized": None,
        "vol_rel": None,
    }
    base.update(extra)
    return IndicatorSnapshot(**base)


def test_returns_none_when_emas_not_warmed_up() -> None:
    assert compute_trend(_snapshot(None, None)) is None
    assert compute_trend(_snapshot(100.0, None)) is None


def test_uptrend_direction_strength_and_slope() -> None:
    result = compute_trend(_snapshot(110.0, 100.0, ema_slow_prev=95.0))
    assert result is not None
    assert result.direction == "up"
    assert result.strength == 100.0  # 10% separation, scale=20 -> saturates at 100
    assert result.score == 100.0
    assert result.slope > 0


def test_downtrend_direction_and_signed_score() -> None:
    result = compute_trend(_snapshot(90.0, 100.0, ema_slow_prev=100.0))
    assert result is not None
    assert result.direction == "down"
    assert result.strength == 100.0
    assert result.score == -100.0
    assert result.slope == 0.0


def test_flat_when_separation_below_threshold() -> None:
    result = compute_trend(_snapshot(100.01, 100.0, ema_slow_prev=100.0))
    assert result is not None
    assert result.direction == "flat"
    assert result.score == 0.0
    assert result.strength < 1.0
