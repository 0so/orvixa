"""Decision/policy overlay on top of :mod:`orvixa.backtest.regime_validation`.

This module is a *third pass*: a pure dict-to-dict transformation over the
output of :func:`~orvixa.backtest.regime_validation.run_regime_validation`
(which itself wraps :func:`~orvixa.backtest.signal_validation.run_signal_validation`
unmodified). It introduces **no new statistics** -- every value it reads
(``classification``, ``edge``, ``edge_ci_low/high``, ``clustered_fraction``,
``neff``, and the global ``edge``) already exists in its inputs.

For each ``(symbol, regime_bucket, signal_type)`` it derives one
ALLOW / BLOCK / REDUCE decision at a single ``decision_horizon``:

- **ALLOW**: "no instability or red flags detected" in the available
  evidence -- *not* a claim of positive alpha.
- **BLOCK**: too little effectively-independent evidence in this regime to
  say anything.
- **REDUCE**: some evidence exists but it is unstable (CI spans zero,
  occurrences are mostly clustered) or it disagrees with the global
  estimator.

All three are risk/uncertainty *flags*, not sizing or execution
recommendations -- this layer does not know about positions, fees, or
slippage. The global edge is used only as a reference estimator for a
directional-agreement check, never as ground truth.
"""

from __future__ import annotations

from typing import Any

from .regime_validation import run_regime_validation

DEFAULT_DECISION_HORIZON: int = 1

ALLOW = "ALLOW"
BLOCK = "BLOCK"
REDUCE = "REDUCE"

_REASON_INSUFFICIENT_DATA = "insufficient effective sample size in this regime"
_REASON_EDGE_CI_SPANS_ZERO = "regime-conditioned edge CI includes zero"
_REASON_CLUSTERED = "signal occurrences in this regime are mostly clustered"
_REASON_AGREES_WITH_GLOBAL = "regime-conditioned edge agrees with the global edge's direction"
_REASON_DIVERGES_FROM_GLOBAL = "regime-conditioned edge diverges from the global edge's direction"
_REASON_NO_GLOBAL_REFERENCE = "no global edge reference is available for comparison"

# Above this fraction of clustered (non-isolated) occurrences, a
# "diagnostic_only" cell is attributed to clustering rather than CI width.
_CLUSTER_DOMINANCE_THRESHOLD = 0.5


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _decide(
    regime_cell: dict[str, Any],
    global_edge: float | None,
    decision_horizon: int,
) -> dict[str, Any]:
    """One ALLOW/BLOCK/REDUCE decision for a single regime-bucket/signal-type cell."""
    classification = regime_cell["classification"][decision_horizon]
    regime_edge = regime_cell["edge"][decision_horizon]

    if classification == "insufficient_data":
        decision, reason = BLOCK, _REASON_INSUFFICIENT_DATA
    elif classification == "diagnostic_only":
        ci_low = regime_cell["edge_ci_low"][decision_horizon]
        ci_high = regime_cell["edge_ci_high"][decision_horizon]
        ci_spans_zero = ci_low is None or ci_high is None or (ci_low <= 0.0 <= ci_high)
        if ci_spans_zero:
            decision, reason = REDUCE, _REASON_EDGE_CI_SPANS_ZERO
        elif regime_cell["clustered_fraction"] > _CLUSTER_DOMINANCE_THRESHOLD:
            decision, reason = REDUCE, _REASON_CLUSTERED
        else:
            # Defensive fallback: classification is "diagnostic_only" but
            # neither known sub-condition holds (shouldn't normally happen).
            decision, reason = REDUCE, _REASON_EDGE_CI_SPANS_ZERO
    elif classification == "regime_conditional_candidate":
        if global_edge is None:
            decision, reason = REDUCE, _REASON_NO_GLOBAL_REFERENCE
        elif _sign(regime_edge) == _sign(global_edge) and _sign(regime_edge) != 0:
            decision, reason = ALLOW, _REASON_AGREES_WITH_GLOBAL
        else:
            decision, reason = REDUCE, _REASON_DIVERGES_FROM_GLOBAL
    else:
        # Unknown classification label -- fail safe.
        decision, reason = BLOCK, _REASON_INSUFFICIENT_DATA

    return {
        "decision": decision,
        "reason": reason,
        "regime_classification": classification,
        "regime_edge": regime_edge,
        "global_edge": global_edge,
        "sample_size": regime_cell["sample_size"],
        "neff": regime_cell["neff"][decision_horizon],
    }


def compute_policy_decisions(
    result: dict[str, Any],
    decision_horizon: int = DEFAULT_DECISION_HORIZON,
) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
    """Derive ``{symbol: {bucket_key: {signal_type: decision}}}`` from ``result``.

    ``result`` is the dict returned by
    :func:`~orvixa.backtest.regime_validation.run_regime_validation` (or any
    dict with the same ``metrics`` / ``regime_metrics`` shape) -- read-only,
    never mutated or reinterpreted.
    """
    policy_decisions: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for symbol, regime_metrics in result["regime_metrics"].items():
        symbol_edges = result["metrics"].get("edge", {})
        policy_decisions[symbol] = {}
        for bucket_key, by_type in regime_metrics.items():
            policy_decisions[symbol][bucket_key] = {}
            for sig_type, regime_cell in by_type.items():
                global_edge = symbol_edges.get(sig_type, {}).get(decision_horizon)
                policy_decisions[symbol][bucket_key][sig_type] = _decide(
                    regime_cell, global_edge, decision_horizon
                )

    return policy_decisions


async def run_policy_validation(
    pool: Any,
    settings: Any,
    symbols: Any,
    *args: Any,
    decision_horizon: int = DEFAULT_DECISION_HORIZON,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run :func:`~orvixa.backtest.regime_validation.run_regime_validation`, then add ``policy_decisions``.

    The returned dict contains everything ``run_regime_validation`` returns
    (``signals``, ``metrics``, ``dataset_type``, ``mode``, ``regime_metrics``),
    unchanged, plus a new top-level ``policy_decisions`` key -- see
    :func:`compute_policy_decisions`.
    """
    result = await run_regime_validation(pool, settings, symbols, *args, **kwargs)
    policy_decisions = compute_policy_decisions(result, decision_horizon)
    return {**result, "policy_decisions": policy_decisions}
