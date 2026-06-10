"""Market Regime Engine — Risk-On / Risk-Off / Rotational + volatility regime.

Combines :class:`~orvixa.symbols.breadth.BreadthEngine`'s output (reused
as-is from M3 -- it already consumes the feed's whole-market
``on_market_snapshot`` stream) with cross-symbol trend participation from
:mod:`orvixa.analytics.trend` to classify the overall market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..symbols.models import BreadthSnapshot
from .health import compute_health_score

# Risk-On / Risk-Off thresholds on the advance/decline ratio and the
# percentage of symbols trading above their rolling-average trend.
_RISK_ON_AD_RATIO = 1.2
_RISK_ON_PCT_ABOVE_TREND = 55.0
_RISK_OFF_AD_RATIO = 0.8
_RISK_OFF_PCT_ABOVE_TREND = 45.0


@dataclass(slots=True)
class RegimeResult:
    """Ready to persist as a :class:`~orvixa.db.models.MarketMemoryRow`."""

    regime: str  # "risk_on" | "risk_off" | "rotational"
    vol_regime: str  # "low" | "normal" | "high"
    breadth: float  # ad_ratio
    health_score: int  # 0-100
    snapshot: dict[str, Any] = field(default_factory=dict)


class RegimeEngine:
    """Stateless classifier — one snapshot in, one :class:`RegimeResult` out."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def evaluate(
        self,
        breadth: BreadthSnapshot,
        trend_up_frac: float,
        trend_down_frac: float,
        avg_vol_realized: float | None,
    ) -> RegimeResult:
        if (
            breadth.ad_ratio >= _RISK_ON_AD_RATIO
            and breadth.pct_above_trend >= _RISK_ON_PCT_ABOVE_TREND
            and trend_up_frac > trend_down_frac
        ):
            regime = "risk_on"
        elif (
            breadth.ad_ratio <= _RISK_OFF_AD_RATIO
            and breadth.pct_above_trend <= _RISK_OFF_PCT_ABOVE_TREND
            and trend_down_frac > trend_up_frac
        ):
            regime = "risk_off"
        else:
            regime = "rotational"

        vol_regime = self._vol_regime(avg_vol_realized)
        health_score = compute_health_score(breadth, trend_up_frac, vol_regime)

        snapshot = {
            "ad_ratio": breadth.ad_ratio,
            "pct_above_trend": breadth.pct_above_trend,
            "advancers": breadth.advancers,
            "decliners": breadth.decliners,
            "unchanged": breadth.unchanged,
            "new_highs": breadth.new_highs,
            "new_lows": breadth.new_lows,
            "trend_up_frac": round(trend_up_frac, 4),
            "trend_down_frac": round(trend_down_frac, 4),
            "avg_vol_realized": avg_vol_realized,
        }

        return RegimeResult(
            regime=regime,
            vol_regime=vol_regime,
            breadth=breadth.ad_ratio,
            health_score=health_score,
            snapshot=snapshot,
        )

    def _vol_regime(self, avg_vol_realized: float | None) -> str:
        if avg_vol_realized is None:
            return "normal"
        threshold = self._settings.high_volatility_pct
        if avg_vol_realized >= threshold:
            return "high"
        if avg_vol_realized <= threshold / 3.0:
            return "low"
        return "normal"
