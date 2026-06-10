"""BUY PRESSURE / SELL PRESSURE / HIGH VOLATILITY signal generation.

:class:`SignalEngine` keeps two small per-symbol state machines (pressure:
``"neutral"|"buy"|"sell"``, volatility: ``"normal"|"high"``) so a signal is
only emitted on a *transition*, not on every candle — matching ``signals``'
``state_from``/``state_to`` columns and avoiding a flood of duplicate rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from .indicators import IndicatorSnapshot
from .trend import TrendResult

# RSI bands beyond which a same-direction pressure signal is suppressed
# (an "up" trend with RSI >= 70 is overbought; a "down" trend with RSI <= 30
# is oversold) -- avoids piling onto an already-extended move.
_RSI_OVERBOUGHT = 70.0
_RSI_OVERSOLD = 30.0


@dataclass(slots=True)
class SignalResult:
    """A signal ready to persist as a :class:`~orvixa.db.models.SignalRow`."""

    type: str  # "buy" | "sell" | "highvol"
    confidence: int  # 0-100
    score: float
    components: dict[str, Any] = field(default_factory=dict)
    state_from: str | None = None
    state_to: str = ""


class SignalEngine:
    """Stateful (per ``symbol_id``) BUY/SELL PRESSURE + HIGH VOLATILITY classifier."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pressure_state: dict[int, str] = {}
        self._highvol_state: dict[int, str] = {}

    def evaluate(
        self, symbol_id: int, snapshot: IndicatorSnapshot, trend: TrendResult | None
    ) -> list[SignalResult]:
        """Return zero or more signals for this candle (state transitions only)."""
        results: list[SignalResult] = []

        pressure = self._evaluate_pressure(symbol_id, snapshot, trend)
        if pressure is not None:
            results.append(pressure)

        highvol = self._evaluate_highvol(symbol_id, snapshot)
        if highvol is not None:
            results.append(highvol)

        return results

    def _evaluate_pressure(
        self, symbol_id: int, snapshot: IndicatorSnapshot, trend: TrendResult | None
    ) -> SignalResult | None:
        if trend is None or snapshot.rsi is None or snapshot.vol_rel is None:
            return None

        if trend.direction == "up" and snapshot.rsi < _RSI_OVERBOUGHT:
            new_state = "buy"
        elif trend.direction == "down" and snapshot.rsi > _RSI_OVERSOLD:
            new_state = "sell"
        else:
            new_state = "neutral"

        old_state = self._pressure_state.get(symbol_id, "neutral")

        if new_state == "neutral":
            self._pressure_state[symbol_id] = "neutral"
            return None

        if new_state == old_state:
            return None

        confidence = self._pressure_confidence(trend, snapshot)
        if confidence < self._settings.signal_min_confidence:
            # Don't update state -- retry on a future candle once confidence
            # crosses the threshold instead of getting stuck in old_state.
            return None

        self._pressure_state[symbol_id] = new_state
        return SignalResult(
            type=new_state,
            confidence=confidence,
            score=trend.score,
            components={
                "trend_direction": trend.direction,
                "trend_strength": round(trend.strength, 2),
                "trend_score": round(trend.score, 2),
                "rsi": round(snapshot.rsi, 2),
                "vol_rel": round(snapshot.vol_rel, 2),
            },
            state_from=old_state,
            state_to=new_state,
        )

    def _pressure_confidence(self, trend: TrendResult, snapshot: IndicatorSnapshot) -> int:
        assert snapshot.rsi is not None and snapshot.vol_rel is not None
        rsi_component = min(abs(snapshot.rsi - 50.0) * 2.0, 100.0)
        vol_component = min(max((snapshot.vol_rel - 1.0) * 50.0, 0.0), 100.0)
        confidence = 0.6 * trend.strength + 0.2 * rsi_component + 0.2 * vol_component
        return int(round(min(max(confidence, 0.0), 100.0)))

    def _evaluate_highvol(self, symbol_id: int, snapshot: IndicatorSnapshot) -> SignalResult | None:
        if snapshot.vol_realized is None:
            return None

        threshold = self._settings.high_volatility_pct
        is_high = snapshot.vol_realized >= threshold
        old_state = self._highvol_state.get(symbol_id, "normal")

        if not is_high:
            self._highvol_state[symbol_id] = "normal"
            return None

        if old_state == "high":
            return None

        confidence = int(round(min(snapshot.vol_realized / threshold * 50.0, 100.0)))
        if confidence < self._settings.signal_min_confidence:
            # Don't update state -- retry on a future candle once confidence
            # crosses the threshold instead of getting stuck in old_state.
            return None

        self._highvol_state[symbol_id] = "high"
        return SignalResult(
            type="highvol",
            confidence=confidence,
            score=snapshot.vol_realized,
            components={
                "vol_realized": round(snapshot.vol_realized, 4),
                "threshold": threshold,
            },
            state_from=old_state,
            state_to="high",
        )
