"""Milestone 4 — deterministic analytics engine.

Pure, rule-based math over closed candles and whole-market ticker snapshots:
incremental indicators (EMA/RSI/ATR/realized volatility/relative volume),
trend direction/strength/slope, BUY/SELL PRESSURE + HIGH VOLATILITY signals,
breakout/breakdown/pump/dump/vol_spike events, and market regime/health.

No AI/LLM/embeddings/agents/external services anywhere in this package —
every output is a deterministic function of the rolling per-symbol state
and configured thresholds (:mod:`orvixa.config`).

:class:`~orvixa.analytics.engine.AnalyticsEngine` is the orchestrator wired
by :mod:`orvixa.runners.analytics`, the same way M2's ``CandleSink`` and M3's
``SymbolManager`` are wired into their runners.
"""

from __future__ import annotations

from .engine import AnalyticsEngine, indicator_repository_sink
from .events import EventEngine, EventResult
from .health import compute_health_score
from .indicators import (
    EMA,
    IndicatorSnapshot,
    RealizedVolatility,
    RelativeVolume,
    SymbolIndicators,
    WilderATR,
    WilderRSI,
)
from .regime import RegimeEngine, RegimeResult
from .signals import SignalEngine, SignalResult
from .trend import TrendResult, compute_trend

__all__ = [
    "AnalyticsEngine",
    "indicator_repository_sink",
    "EMA",
    "WilderRSI",
    "WilderATR",
    "RealizedVolatility",
    "RelativeVolume",
    "IndicatorSnapshot",
    "SymbolIndicators",
    "TrendResult",
    "compute_trend",
    "SignalEngine",
    "SignalResult",
    "EventEngine",
    "EventResult",
    "RegimeEngine",
    "RegimeResult",
    "compute_health_score",
]
