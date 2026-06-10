"""Tests for :class:`orvixa.analytics.regime.RegimeEngine` (M4)."""

from __future__ import annotations

from orvixa.analytics.regime import RegimeEngine
from orvixa.config import Settings
from orvixa.symbols.models import BreadthSnapshot


def _breadth(ad_ratio: float, pct_above_trend: float) -> BreadthSnapshot:
    return BreadthSnapshot(
        total=10,
        advancers=7,
        decliners=3,
        unchanged=0,
        ad_ratio=ad_ratio,
        pct_above_trend=pct_above_trend,
        new_highs=2,
        new_lows=0,
    )


def _settings(**overrides) -> Settings:
    defaults = {"high_volatility_pct": 3.0}
    defaults.update(overrides)
    return Settings(**defaults)


def test_risk_on_when_breadth_and_participation_strong() -> None:
    engine = RegimeEngine(_settings())
    result = engine.evaluate(_breadth(ad_ratio=2.0, pct_above_trend=70.0), trend_up_frac=0.8, trend_down_frac=0.1, avg_vol_realized=1.0)
    assert result.regime == "risk_on"
    assert result.vol_regime == "low"
    assert result.breadth == 2.0
    assert 0 <= result.health_score <= 100
    assert result.snapshot["ad_ratio"] == 2.0


def test_risk_off_when_breadth_and_participation_weak() -> None:
    engine = RegimeEngine(_settings())
    result = engine.evaluate(_breadth(ad_ratio=0.5, pct_above_trend=30.0), trend_up_frac=0.1, trend_down_frac=0.8, avg_vol_realized=5.0)
    assert result.regime == "risk_off"
    assert result.vol_regime == "high"


def test_rotational_when_mixed() -> None:
    engine = RegimeEngine(_settings())
    result = engine.evaluate(_breadth(ad_ratio=1.0, pct_above_trend=50.0), trend_up_frac=0.5, trend_down_frac=0.5, avg_vol_realized=1.5)
    assert result.regime == "rotational"
    assert result.vol_regime == "normal"


def test_vol_regime_normal_when_no_data() -> None:
    engine = RegimeEngine(_settings())
    result = engine.evaluate(_breadth(ad_ratio=1.0, pct_above_trend=50.0), trend_up_frac=0.5, trend_down_frac=0.5, avg_vol_realized=None)
    assert result.vol_regime == "normal"
    assert result.snapshot["avg_vol_realized"] is None
