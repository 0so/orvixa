"""Breakout / breakdown / pump / dump / volatility-spike event detection.

:class:`EventEngine` keeps small bounded ``deque``s per ``symbol_id`` —
prior highs/lows (for breakout/breakdown), prior closes (for pump/dump), and
prior realized-volatility readings (for vol_spike baseline). Every check
compares the *current* candle against history that excludes it; history is
updated only after the checks run.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from .indicators import IndicatorSnapshot


@dataclass(slots=True)
class EventResult:
    """A market event ready to persist as a :class:`~orvixa.db.models.MarketEventRow`."""

    type: str  # "breakout" | "breakdown" | "pump" | "dump" | "vol_spike"
    magnitude: float | None
    severity: int | None
    price: float
    payload: dict[str, Any] = field(default_factory=dict)


def _severity(ratio: float) -> int:
    """Map a magnitude/threshold ratio to a 1-3 severity bucket."""
    if ratio >= 3.0:
        return 3
    if ratio >= 1.5:
        return 2
    return 1


class EventEngine:
    """Stateful (per ``symbol_id``) detector for the M4 market-event types."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._highs: dict[int, deque[float]] = {}
        self._lows: dict[int, deque[float]] = {}
        self._closes: dict[int, deque[float]] = {}
        self._vol_history: dict[int, deque[float]] = {}

        # Per-symbol state machines so an event is only emitted on a
        # *transition* into the triggering condition, not on every candle
        # that the condition continues to hold (avoids event spam).
        self._breakout_state: dict[int, str] = {}  # "none" | "up" | "down"
        self._pump_dump_state: dict[int, str] = {}  # "none" | "pump" | "dump"
        self._vol_spike_state: dict[int, str] = {}  # "none" | "spike"

    def evaluate(self, symbol_id: int, snapshot: IndicatorSnapshot) -> list[EventResult]:
        """Return zero or more events for this candle, then advance rolling history."""
        results: list[EventResult] = []

        breakout = self._check_breakout(symbol_id, snapshot)
        if breakout is not None:
            results.append(breakout)

        pump_dump = self._check_pump_dump(symbol_id, snapshot)
        if pump_dump is not None:
            results.append(pump_dump)

        vol_spike = self._check_vol_spike(symbol_id, snapshot)
        if vol_spike is not None:
            results.append(vol_spike)

        self._update_history(symbol_id, snapshot)
        return results

    def _check_breakout(self, symbol_id: int, snapshot: IndicatorSnapshot) -> EventResult | None:
        highs = self._highs.get(symbol_id)
        lows = self._lows.get(symbol_id)
        window = self._settings.breakout_window
        if highs is None or lows is None or len(highs) < window:
            return None

        prior_high = max(highs)
        prior_low = min(lows)
        old_state = self._breakout_state.get(symbol_id, "none")

        if snapshot.close > prior_high:
            if old_state == "up":
                return None
            self._breakout_state[symbol_id] = "up"
            magnitude = (snapshot.close - prior_high) / prior_high * 100.0 if prior_high > 0 else 0.0
            return EventResult(
                type="breakout",
                magnitude=magnitude,
                severity=_severity(abs(magnitude)),
                price=snapshot.close,
                payload={"level": prior_high, "window": window},
            )

        if snapshot.close < prior_low:
            if old_state == "down":
                return None
            self._breakout_state[symbol_id] = "down"
            magnitude = (prior_low - snapshot.close) / prior_low * 100.0 if prior_low > 0 else 0.0
            return EventResult(
                type="breakdown",
                magnitude=magnitude,
                severity=_severity(abs(magnitude)),
                price=snapshot.close,
                payload={"level": prior_low, "window": window},
            )

        self._breakout_state[symbol_id] = "none"
        return None

    def _check_pump_dump(self, symbol_id: int, snapshot: IndicatorSnapshot) -> EventResult | None:
        closes = self._closes.get(symbol_id)
        window = self._settings.pump_dump_window
        if closes is None or len(closes) < window:
            return None

        ref = closes[0]
        if ref <= 0:
            return None

        pct = (snapshot.close - ref) / ref * 100.0
        threshold = self._settings.pump_dump_pct
        old_state = self._pump_dump_state.get(symbol_id, "none")

        if pct >= threshold:
            if old_state == "pump":
                return None
            self._pump_dump_state[symbol_id] = "pump"
            return EventResult(
                type="pump",
                magnitude=pct,
                severity=_severity(pct / threshold),
                price=snapshot.close,
                payload={"window": window, "pct": round(pct, 4)},
            )

        if pct <= -threshold:
            if old_state == "dump":
                return None
            self._pump_dump_state[symbol_id] = "dump"
            return EventResult(
                type="dump",
                magnitude=pct,
                severity=_severity(abs(pct) / threshold),
                price=snapshot.close,
                payload={"window": window, "pct": round(pct, 4)},
            )

        self._pump_dump_state[symbol_id] = "none"
        return None

    def _check_vol_spike(self, symbol_id: int, snapshot: IndicatorSnapshot) -> EventResult | None:
        if snapshot.vol_realized is None:
            return None

        history = self._vol_history.get(symbol_id)
        window = self._settings.vol_spike_window
        if history is None or len(history) < window:
            return None

        baseline = sum(history) / len(history)
        if baseline <= 0:
            return None

        multiplier = self._settings.vol_spike_multiplier
        old_state = self._vol_spike_state.get(symbol_id, "none")

        if snapshot.vol_realized >= baseline * multiplier:
            if old_state == "spike":
                return None
            self._vol_spike_state[symbol_id] = "spike"
            return EventResult(
                type="vol_spike",
                magnitude=snapshot.vol_realized,
                severity=_severity(snapshot.vol_realized / (baseline * multiplier)),
                price=snapshot.close,
                payload={
                    "baseline": round(baseline, 4),
                    "multiplier": multiplier,
                },
            )

        self._vol_spike_state[symbol_id] = "none"
        return None

    def _update_history(self, symbol_id: int, snapshot: IndicatorSnapshot) -> None:
        self._highs.setdefault(symbol_id, deque(maxlen=self._settings.breakout_window)).append(
            snapshot.high
        )
        self._lows.setdefault(symbol_id, deque(maxlen=self._settings.breakout_window)).append(
            snapshot.low
        )
        self._closes.setdefault(symbol_id, deque(maxlen=self._settings.pump_dump_window)).append(
            snapshot.close
        )
        if snapshot.vol_realized is not None:
            self._vol_history.setdefault(
                symbol_id, deque(maxlen=self._settings.vol_spike_window)
            ).append(snapshot.vol_realized)
