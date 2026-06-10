"""Tests for :class:`orvixa.analytics.signals.SignalEngine` (M4)."""

from __future__ import annotations

from orvixa.analytics.indicators import IndicatorSnapshot
from orvixa.analytics.signals import SignalEngine
from orvixa.analytics.trend import TrendResult
from orvixa.config import Settings


def _snapshot(rsi=None, vol_realized=None, vol_rel=None) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        close=100.0,
        high=101.0,
        low=99.0,
        volume=10.0,
        ema_fast=110.0,
        ema_slow=100.0,
        ema_slow_prev=95.0,
        rsi=rsi,
        atr=1.0,
        vol_realized=vol_realized,
        vol_rel=vol_rel,
    )


def _settings(**overrides) -> Settings:
    defaults = {"signal_min_confidence": 60, "high_volatility_pct": 3.0}
    defaults.update(overrides)
    return Settings(**defaults)


def test_buy_pressure_emitted_on_transition_then_suppressed() -> None:
    engine = SignalEngine(_settings())
    trend = TrendResult(direction="up", strength=100.0, slope=1.0, score=100.0)
    snapshot = _snapshot(rsi=60.0, vol_realized=1.0, vol_rel=2.0)

    results = engine.evaluate(symbol_id=1, snapshot=snapshot, trend=trend)
    buy = [r for r in results if r.type == "buy"]
    assert len(buy) == 1
    assert buy[0].state_from == "neutral"
    assert buy[0].state_to == "buy"
    assert buy[0].confidence >= 60
    assert "trend_direction" in buy[0].components

    # Same trend/snapshot again -> already in "buy" state, no repeat signal.
    results2 = engine.evaluate(symbol_id=1, snapshot=snapshot, trend=trend)
    assert [r for r in results2 if r.type == "buy"] == []


def test_sell_pressure_on_downtrend() -> None:
    engine = SignalEngine(_settings())
    trend = TrendResult(direction="down", strength=100.0, slope=-1.0, score=-100.0)
    snapshot = _snapshot(rsi=40.0, vol_realized=1.0, vol_rel=2.0)

    results = engine.evaluate(symbol_id=1, snapshot=snapshot, trend=trend)
    sell = [r for r in results if r.type == "sell"]
    assert len(sell) == 1
    assert sell[0].state_to == "sell"


def test_overbought_uptrend_suppressed() -> None:
    engine = SignalEngine(_settings())
    trend = TrendResult(direction="up", strength=100.0, slope=1.0, score=100.0)
    snapshot = _snapshot(rsi=75.0, vol_realized=1.0, vol_rel=2.0)

    results = engine.evaluate(symbol_id=1, snapshot=snapshot, trend=trend)
    assert [r for r in results if r.type in ("buy", "sell")] == []


def test_low_confidence_suppressed() -> None:
    engine = SignalEngine(_settings(signal_min_confidence=99))
    trend = TrendResult(direction="up", strength=10.0, slope=1.0, score=10.0)
    snapshot = _snapshot(rsi=51.0, vol_realized=1.0, vol_rel=1.0)

    results = engine.evaluate(symbol_id=1, snapshot=snapshot, trend=trend)
    assert [r for r in results if r.type in ("buy", "sell")] == []


def test_high_volatility_signal_on_transition() -> None:
    engine = SignalEngine(_settings())
    trend = None
    snapshot_high = _snapshot(rsi=None, vol_realized=5.0, vol_rel=None)

    results = engine.evaluate(symbol_id=1, snapshot=snapshot_high, trend=trend)
    highvol = [r for r in results if r.type == "highvol"]
    assert len(highvol) == 1
    assert highvol[0].state_from == "normal"
    assert highvol[0].state_to == "high"
    assert highvol[0].confidence >= 60

    # Stays high -> no repeat.
    results2 = engine.evaluate(symbol_id=1, snapshot=snapshot_high, trend=trend)
    assert [r for r in results2 if r.type == "highvol"] == []

    # Drops back to normal -> state transition recorded but not persisted.
    snapshot_normal = _snapshot(rsi=None, vol_realized=1.0, vol_rel=None)
    results3 = engine.evaluate(symbol_id=1, snapshot=snapshot_normal, trend=trend)
    assert [r for r in results3 if r.type == "highvol"] == []


def test_low_confidence_transition_does_not_block_later_signal() -> None:
    """A low-confidence "buy" transition must not get permanently stuck.

    Regression test: previously the pressure state was updated to "buy"
    *before* the confidence gate, so once a low-confidence "buy" candle was
    seen, a later high-confidence "buy" candle would be suppressed forever
    (new_state == old_state == "buy").
    """
    engine = SignalEngine(_settings(signal_min_confidence=60))

    # confidence ~= 6 (well below 60) -> suppressed, state must stay "neutral".
    low_conf_trend = TrendResult(direction="up", strength=10.0, slope=1.0, score=10.0)
    low_conf_snapshot = _snapshot(rsi=51.0, vol_realized=1.0, vol_rel=1.0)
    results1 = engine.evaluate(symbol_id=1, snapshot=low_conf_snapshot, trend=low_conf_trend)
    assert [r for r in results1 if r.type == "buy"] == []

    # confidence ~= 74 (>= 60) -> must now emit, since state was never moved
    # to "buy" by the suppressed candle above.
    high_conf_trend = TrendResult(direction="up", strength=100.0, slope=1.0, score=100.0)
    high_conf_snapshot = _snapshot(rsi=60.0, vol_realized=1.0, vol_rel=2.0)
    results2 = engine.evaluate(symbol_id=1, snapshot=high_conf_snapshot, trend=high_conf_trend)
    buy = [r for r in results2 if r.type == "buy"]
    assert len(buy) == 1
    assert buy[0].state_from == "neutral"
    assert buy[0].state_to == "buy"


def test_low_confidence_highvol_transition_does_not_block_later_signal() -> None:
    """Same regression as above, for the HIGH VOLATILITY state machine."""
    engine = SignalEngine(_settings(signal_min_confidence=60, high_volatility_pct=3.0))

    # confidence ~= 58 (< 60) -> suppressed, state must stay "normal".
    results1 = engine.evaluate(symbol_id=1, snapshot=_snapshot(vol_realized=3.5), trend=None)
    assert [r for r in results1 if r.type == "highvol"] == []

    # confidence ~= 67 (>= 60) -> must now emit, since state was never moved
    # to "high" by the suppressed candle above.
    results2 = engine.evaluate(symbol_id=1, snapshot=_snapshot(vol_realized=4.0), trend=None)
    highvol = [r for r in results2 if r.type == "highvol"]
    assert len(highvol) == 1
    assert highvol[0].state_from == "normal"
    assert highvol[0].state_to == "high"


def test_missing_inputs_skip_pressure_signal() -> None:
    engine = SignalEngine(_settings())
    trend = TrendResult(direction="up", strength=100.0, slope=1.0, score=100.0)
    snapshot = _snapshot(rsi=None, vol_realized=1.0, vol_rel=None)

    results = engine.evaluate(symbol_id=1, snapshot=snapshot, trend=trend)
    assert [r for r in results if r.type in ("buy", "sell")] == []
