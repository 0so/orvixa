"""Incremental, per-symbol indicator state.

Every calculator here is O(1) per update (or O(window) with a bounded
``deque``) and holds exactly the state needed to fold in the next candle —
no full-history recalculation, per M4 requirement #7. :class:`SymbolIndicators`
bundles one of each per tracked symbol; :class:`IndicatorSnapshot` is the
result of folding in one candle.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from ..config import Settings
from ..feeds.base import Candle


class EMA:
    """Exponential moving average, seeded with the first observed price."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._alpha = 2.0 / (period + 1)
        self.value: float | None = None

    def update(self, price: float) -> float:
        if self.value is None:
            self.value = price
        else:
            self.value = (price - self.value) * self._alpha + self.value
        return self.value


class WilderRSI:
    """Wilder's smoothed RSI.

    The first value requires ``period`` price changes (i.e. ``period + 1``
    closes); the seed average gain/loss is the simple mean of those changes,
    then every subsequent update applies Wilder smoothing.
    """

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev_close: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev_close is None:
            self._prev_close = close
            return None

        change = close - self._prev_close
        self._prev_close = close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if self._avg_gain is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) < self.period:
                return None
            self._avg_gain = sum(self._seed_gains) / self.period
            self._avg_loss = sum(self._seed_losses) / self.period
        else:
            assert self._avg_loss is not None
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - (100.0 / (1.0 + rs))
        return self.value


class WilderATR:
    """Wilder's smoothed Average True Range."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._prev_close: float | None = None
        self._seed_trs: list[float] = []
        self._avg_tr: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev_close is None:
            true_range = high - low
        else:
            true_range = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close

        if self._avg_tr is None:
            self._seed_trs.append(true_range)
            if len(self._seed_trs) < self.period:
                return None
            self._avg_tr = sum(self._seed_trs) / self.period
        else:
            self._avg_tr = (self._avg_tr * (self.period - 1) + true_range) / self.period

        self.value = self._avg_tr
        return self.value


class RealizedVolatility:
    """Rolling stdev of log returns over ``window`` closes, expressed as a percent."""

    def __init__(self, window: int = 20) -> None:
        self.window = window
        self._prev_close: float | None = None
        self._returns: deque[float] = deque(maxlen=window)
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev_close is not None and self._prev_close > 0 and close > 0:
            self._returns.append(math.log(close / self._prev_close))
        self._prev_close = close

        if len(self._returns) < self.window:
            return None

        mean = sum(self._returns) / len(self._returns)
        variance = sum((r - mean) ** 2 for r in self._returns) / len(self._returns)
        self.value = math.sqrt(variance) * 100.0
        return self.value


class RelativeVolume:
    """Current candle's volume relative to the average of the prior ``window`` candles."""

    def __init__(self, window: int = 20) -> None:
        self.window = window
        self._volumes: deque[float] = deque(maxlen=window)
        self.value: float | None = None

    def update(self, volume: float) -> float | None:
        if len(self._volumes) < self.window or sum(self._volumes) <= 0:
            self.value = None
        else:
            avg = sum(self._volumes) / len(self._volumes)
            self.value = volume / avg if avg > 0 else None

        self._volumes.append(volume)
        return self.value


@dataclass(slots=True)
class IndicatorSnapshot:
    """Result of folding one closed candle into a symbol's indicator state."""

    close: float
    high: float
    low: float
    volume: float
    ema_fast: float | None
    ema_slow: float | None
    ema_slow_prev: float | None
    rsi: float | None
    atr: float | None
    vol_realized: float | None
    vol_rel: float | None


class SymbolIndicators:
    """Bundles one of each incremental calculator for a single symbol."""

    def __init__(self, settings: Settings) -> None:
        self.ema_fast = EMA(settings.ema_fast_period)
        self.ema_slow = EMA(settings.ema_slow_period)
        self.rsi = WilderRSI(settings.rsi_period)
        self.atr = WilderATR(settings.atr_period)
        self.vol_realized = RealizedVolatility(settings.realized_vol_window)
        self.vol_rel = RelativeVolume(settings.relative_volume_window)

    def update(self, candle: Candle) -> IndicatorSnapshot:
        ema_slow_prev = self.ema_slow.value
        return IndicatorSnapshot(
            close=candle.close,
            high=candle.high,
            low=candle.low,
            volume=candle.volume,
            ema_fast=self.ema_fast.update(candle.close),
            ema_slow=self.ema_slow.update(candle.close),
            ema_slow_prev=ema_slow_prev,
            rsi=self.rsi.update(candle.close),
            atr=self.atr.update(candle.high, candle.low, candle.close),
            vol_realized=self.vol_realized.update(candle.close),
            vol_rel=self.vol_rel.update(candle.volume),
        )
