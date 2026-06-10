"""Tests for the incremental indicator calculators (M4)."""

from __future__ import annotations

import math

from orvixa.analytics.indicators import (
    EMA,
    RealizedVolatility,
    RelativeVolume,
    SymbolIndicators,
    WilderATR,
    WilderRSI,
)
from orvixa.config import Settings
from orvixa.feeds.base import Candle


def _candle(ts: int, o: float, h: float, low: float, c: float, v: float) -> Candle:
    return Candle(
        symbol="BTC",
        ts=ts,
        open=o,
        high=h,
        low=low,
        close=c,
        volume=v,
        quote_volume=v * c,
        trades=1,
        closed=True,
    )


def test_ema_seeds_with_first_value_then_smooths() -> None:
    ema = EMA(period=3)  # alpha = 0.5
    assert ema.update(10.0) == 10.0
    assert ema.update(20.0) == 15.0
    assert ema.update(10.0) == 12.5


def test_wilder_rsi_warms_up_then_computes() -> None:
    rsi = WilderRSI(period=2)
    assert rsi.update(1.0) is None  # seeds prev_close
    assert rsi.update(2.0) is None  # 1st change, still seeding (need 2)
    value = rsi.update(1.0)  # 2nd change -> seed avg_gain/avg_loss complete
    assert value == 50.0


def test_wilder_atr_warms_up_then_computes() -> None:
    atr = WilderATR(period=2)
    assert atr.update(high=10, low=8, close=9) is None  # seed TR #1 (=2)
    value = atr.update(high=11, low=9, close=10)  # seed TR #2 (=2)
    assert value == 2.0


def test_realized_volatility_warms_up_then_computes() -> None:
    vol = RealizedVolatility(window=2)
    assert vol.update(100.0) is None  # no return yet
    assert vol.update(105.0) is None  # 1 return, window=2
    value = vol.update(100.0)  # 2nd return
    assert value is not None
    expected = abs(math.log(105.0 / 100.0)) * 100.0
    assert math.isclose(value, expected, rel_tol=1e-9)


def test_relative_volume_warms_up_then_computes() -> None:
    rel_vol = RelativeVolume(window=2)
    assert rel_vol.update(10.0) is None
    assert rel_vol.update(20.0) is None
    value = rel_vol.update(30.0)  # avg of [10, 20] = 15 -> 30/15 = 2.0
    assert value == 2.0


def test_symbol_indicators_update_returns_snapshot() -> None:
    settings = Settings(ema_fast_period=2, ema_slow_period=3)
    state = SymbolIndicators(settings)

    snapshot = state.update(_candle(0, 100, 101, 99, 100, 10))
    assert snapshot.close == 100
    assert snapshot.ema_fast == 100.0
    assert snapshot.ema_slow == 100.0
    assert snapshot.ema_slow_prev is None
    # RSI/ATR need more than one candle, vol/relvol need a full window.
    assert snapshot.rsi is None
    assert snapshot.vol_realized is None
    assert snapshot.vol_rel is None

    snapshot2 = state.update(_candle(60_000, 100, 105, 99, 102, 12))
    assert snapshot2.ema_slow_prev == 100.0
    assert snapshot2.ema_fast is not None and snapshot2.ema_fast > 100.0
