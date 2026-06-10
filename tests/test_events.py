"""Tests for :class:`orvixa.analytics.events.EventEngine` (M4)."""

from __future__ import annotations

from orvixa.analytics.events import EventEngine
from orvixa.analytics.indicators import IndicatorSnapshot
from orvixa.config import Settings


def _snapshot(close: float, high: float | None = None, low: float | None = None, vol_realized=None) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        close=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        volume=10.0,
        ema_fast=None,
        ema_slow=None,
        ema_slow_prev=None,
        rsi=None,
        atr=None,
        vol_realized=vol_realized,
        vol_rel=None,
    )


def _settings(**overrides) -> Settings:
    defaults = {
        "breakout_window": 2,
        "pump_dump_window": 2,
        "pump_dump_pct": 50.0,  # high, so pump/dump doesn't accidentally fire in breakout test
        "vol_spike_window": 2,
        "vol_spike_multiplier": 2.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_breakout_and_breakdown_require_full_window() -> None:
    engine = EventEngine(_settings())

    # First two candles just build history (window=2) -- no events yet.
    assert engine.evaluate(1, _snapshot(close=100, high=101, low=99)) == []
    assert engine.evaluate(1, _snapshot(close=101, high=102, low=100)) == []

    # Close above the prior 2-candle high (102) -> breakout.
    events = engine.evaluate(1, _snapshot(close=110, high=110, low=109))
    types = [e.type for e in events]
    assert "breakout" in types
    breakout = next(e for e in events if e.type == "breakout")
    assert breakout.price == 110
    assert breakout.magnitude is not None and breakout.magnitude > 0
    assert breakout.severity in (1, 2, 3)


def test_breakdown_below_prior_low() -> None:
    engine = EventEngine(_settings())
    assert engine.evaluate(1, _snapshot(close=100, high=101, low=99)) == []
    assert engine.evaluate(1, _snapshot(close=99, high=100, low=98)) == []

    events = engine.evaluate(1, _snapshot(close=90, high=91, low=90))
    types = [e.type for e in events]
    assert "breakdown" in types


def test_pump_and_dump_over_window() -> None:
    settings = _settings(breakout_window=10, pump_dump_window=2, pump_dump_pct=5.0)
    engine = EventEngine(settings)

    # Build a 2-candle close history.
    assert engine.evaluate(1, _snapshot(close=100, high=100.5, low=99.5)) == []
    assert engine.evaluate(1, _snapshot(close=101, high=101.5, low=100.5)) == []

    # Close 10% above the close from 2 candles ago (100) -> pump.
    events = engine.evaluate(1, _snapshot(close=110, high=110.5, low=109.5))
    pump = next(e for e in events if e.type == "pump")
    assert pump.magnitude is not None and pump.magnitude > 5.0


def test_dump_over_window() -> None:
    settings = _settings(breakout_window=10, pump_dump_window=2, pump_dump_pct=5.0)
    engine = EventEngine(settings)

    assert engine.evaluate(1, _snapshot(close=100, high=100.5, low=99.5)) == []
    assert engine.evaluate(1, _snapshot(close=99, high=99.5, low=98.5)) == []

    events = engine.evaluate(1, _snapshot(close=90, high=90.5, low=89.5))
    dump = next(e for e in events if e.type == "dump")
    assert dump.magnitude is not None and dump.magnitude < -5.0


def test_volatility_spike_over_baseline() -> None:
    settings = _settings(breakout_window=10, pump_dump_window=10, vol_spike_window=2, vol_spike_multiplier=2.0)
    engine = EventEngine(settings)

    assert engine.evaluate(1, _snapshot(close=100, vol_realized=1.0)) == []
    assert engine.evaluate(1, _snapshot(close=100, vol_realized=1.0)) == []

    # baseline avg = 1.0; 3.0 >= 1.0 * 2.0 -> vol_spike.
    events = engine.evaluate(1, _snapshot(close=100, vol_realized=3.0))
    spike = next(e for e in events if e.type == "vol_spike")
    assert spike.magnitude == 3.0
    assert spike.severity in (1, 2, 3)


def test_breakout_does_not_repeat_until_reset() -> None:
    """Sustained breakouts must emit once, then re-arm only after a reset.

    Regression test for "event spam": previously every candle whose close
    remained above the rolling high re-emitted a duplicate breakout event.
    """
    engine = EventEngine(_settings())

    assert engine.evaluate(1, _snapshot(close=100, high=101, low=99)) == []
    assert engine.evaluate(1, _snapshot(close=101, high=102, low=100)) == []

    events3 = engine.evaluate(1, _snapshot(close=110, high=110, low=109))
    assert "breakout" in [e.type for e in events3]

    # Still above the (rolling) prior high -> must NOT re-emit.
    events4 = engine.evaluate(1, _snapshot(close=115, high=115, low=114))
    assert "breakout" not in [e.type for e in events4]

    # Pulls back into range (prior_high=115, prior_low=109) -> resets state.
    events5 = engine.evaluate(1, _snapshot(close=112, high=113, low=111))
    assert events5 == []

    # New breakout above the (new) rolling high -> fires again.
    events6 = engine.evaluate(1, _snapshot(close=120, high=121, low=119))
    assert "breakout" in [e.type for e in events6]


def test_pump_does_not_repeat_for_sustained_pump() -> None:
    """Sustained pump conditions must emit once, not on every candle."""
    settings = _settings(breakout_window=10, pump_dump_window=2, pump_dump_pct=5.0, vol_spike_window=10)
    engine = EventEngine(settings)

    assert engine.evaluate(1, _snapshot(close=100, high=100.5, low=99.5)) == []
    assert engine.evaluate(1, _snapshot(close=101, high=101.5, low=100.5)) == []

    events3 = engine.evaluate(1, _snapshot(close=110, high=110.5, low=109.5))
    assert "pump" in [e.type for e in events3]

    # Still pumping vs. the rolling reference -> must NOT re-emit.
    events4 = engine.evaluate(1, _snapshot(close=121, high=121.5, low=120.5))
    assert "pump" not in [e.type for e in events4]


def test_vol_spike_does_not_repeat_for_sustained_spike() -> None:
    """Sustained vol-spike conditions must emit once, not on every candle."""
    settings = _settings(breakout_window=10, pump_dump_window=10, vol_spike_window=2, vol_spike_multiplier=2.0)
    engine = EventEngine(settings)

    assert engine.evaluate(1, _snapshot(close=100, vol_realized=1.0)) == []
    assert engine.evaluate(1, _snapshot(close=100, vol_realized=1.0)) == []

    events3 = engine.evaluate(1, _snapshot(close=100, vol_realized=3.0))
    assert "vol_spike" in [e.type for e in events3]

    # Baseline now includes the 3.0 reading (avg=2.0); 10.0 is still a
    # spike vs. that baseline but must NOT re-emit while still elevated.
    events4 = engine.evaluate(1, _snapshot(close=100, vol_realized=10.0))
    assert "vol_spike" not in [e.type for e in events4]


def test_no_events_below_thresholds() -> None:
    settings = _settings(breakout_window=2, pump_dump_window=2, pump_dump_pct=50.0, vol_spike_window=2)
    engine = EventEngine(settings)

    assert engine.evaluate(1, _snapshot(close=100, vol_realized=1.0)) == []
    assert engine.evaluate(1, _snapshot(close=100, vol_realized=1.0)) == []
    assert engine.evaluate(1, _snapshot(close=100, vol_realized=1.0)) == []
