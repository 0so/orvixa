"""Market Health Engine — a single 0-100 score from breadth + trend + volatility."""

from __future__ import annotations

from ..symbols.models import BreadthSnapshot

# An advance/decline ratio of 2:1 or better saturates the breadth component.
_AD_RATIO_SATURATION = 2.0

_HIGH_VOL_PENALTY = 15.0
_LOW_VOL_BONUS = 5.0


def compute_health_score(
    breadth: BreadthSnapshot, trend_up_frac: float, vol_regime: str
) -> int:
    """Combine advance/decline breadth, trend participation, and volatility regime into 0-100."""
    ad_component = min(max(breadth.ad_ratio / _AD_RATIO_SATURATION, 0.0), 1.0) * 100.0
    trend_component_pct = min(max(breadth.pct_above_trend, 0.0), 100.0)
    participation_component = min(max(trend_up_frac, 0.0), 1.0) * 100.0

    score = 0.4 * ad_component + 0.3 * trend_component_pct + 0.3 * participation_component

    if vol_regime == "high":
        score -= _HIGH_VOL_PENALTY
    elif vol_regime == "low":
        score += _LOW_VOL_BONUS

    return int(round(min(max(score, 0.0), 100.0)))
