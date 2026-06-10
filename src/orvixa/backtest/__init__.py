"""M5 — minimal signal-validation harness.

This package answers one question: do M4 signals (BUY/SELL PRESSURE, HIGH
VOLATILITY) precede price moves that differ from "doing nothing"? It replays
historical candles through an unmodified
:class:`~orvixa.analytics.engine.AnalyticsEngine`, captures the
:class:`~orvixa.db.models.SignalRow` rows it emits, and measures forward
returns. No portfolio, strategy, fee, or slippage modeling lives here.

Every run also classifies the dataset as REAL or SYNTHETIC (see
:mod:`orvixa.backtest.dataset`) and refuses ``mode="edge_evaluation"`` on
synthetic data -- see :func:`run_signal_validation`.
"""

from __future__ import annotations

from .dataset import REAL, SYNTHETIC, SYNTHETIC_TAG, classify_dataset
from .policy_validation import ALLOW, BLOCK, REDUCE, compute_policy_decisions, run_policy_validation
from .regime_validation import compute_regime_metrics, run_regime_validation
from .signal_validation import (
    EDGE_EVALUATION,
    PIPELINE_CORRECTNESS,
    VALID_MODES,
    compute_signal_metrics,
    run_signal_validation,
)

__all__ = [
    "compute_signal_metrics",
    "run_signal_validation",
    "compute_regime_metrics",
    "run_regime_validation",
    "compute_policy_decisions",
    "run_policy_validation",
    "ALLOW",
    "BLOCK",
    "REDUCE",
    "classify_dataset",
    "REAL",
    "SYNTHETIC",
    "SYNTHETIC_TAG",
    "PIPELINE_CORRECTNESS",
    "EDGE_EVALUATION",
    "VALID_MODES",
]
