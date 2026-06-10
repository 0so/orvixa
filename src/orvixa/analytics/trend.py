"""Trend direction / strength / slope from the EMA pair.

Pure function of one :class:`~orvixa.analytics.indicators.IndicatorSnapshot`
— no additional state beyond what the snapshot already carries
(``ema_slow_prev`` for the slope).
"""

from __future__ import annotations

from dataclasses import dataclass

from .indicators import IndicatorSnapshot

# EMA-fast/EMA-slow separation (as a % of EMA-slow) below which the trend is
# considered "flat" rather than up/down.
_FLAT_THRESHOLD_PCT = 0.05

# Scales the EMA separation (%) into a 0-100 strength score: a 5% separation
# saturates strength at 100.
_STRENGTH_SCALE = 20.0


@dataclass(slots=True)
class TrendResult:
    """Per-symbol trend snapshot, persisted as ``indicators.trend_score``."""

    direction: str  # "up" | "down" | "flat"
    strength: float  # 0-100, magnitude of the EMA separation
    slope: float  # % change of ema_slow vs. the previous candle
    score: float  # signed strength: +strength (up), -strength (down), 0 (flat)


def compute_trend(snapshot: IndicatorSnapshot) -> TrendResult | None:
    """Derive trend direction/strength/slope/score, or ``None`` if EMAs aren't warmed up yet."""
    if snapshot.ema_fast is None or snapshot.ema_slow is None or snapshot.ema_slow == 0:
        return None

    diff_pct = (snapshot.ema_fast - snapshot.ema_slow) / abs(snapshot.ema_slow) * 100.0

    if diff_pct > _FLAT_THRESHOLD_PCT:
        direction = "up"
    elif diff_pct < -_FLAT_THRESHOLD_PCT:
        direction = "down"
    else:
        direction = "flat"

    strength = min(abs(diff_pct) * _STRENGTH_SCALE, 100.0)
    score = strength if direction == "up" else -strength if direction == "down" else 0.0

    if snapshot.ema_slow_prev is not None and snapshot.ema_slow_prev != 0:
        slope = (snapshot.ema_slow - snapshot.ema_slow_prev) / abs(snapshot.ema_slow_prev) * 100.0
    else:
        slope = 0.0

    return TrendResult(direction=direction, strength=strength, slope=slope, score=score)
