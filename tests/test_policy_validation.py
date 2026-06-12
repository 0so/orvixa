"""Tests for the decision/policy overlay (M5+).

Pure dict-to-dict transformation over ``run_regime_validation``'s output --
reuses the same fixtures as ``test_regime_validation``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fake_pool import FakePool
from orvixa.backtest import (
    ALLOW,
    BLOCK,
    REDUCE,
    compute_policy_decisions,
    run_policy_validation,
    run_regime_validation,
)
from orvixa.config import Settings


def _settings(**overrides) -> Settings:
    defaults = {
        "ema_fast_period": 2,
        "ema_slow_period": 3,
        "rsi_period": 2,
        "atr_period": 2,
        "realized_vol_window": 2,
        "relative_volume_window": 2,
        "breakout_window": 2,
        "pump_dump_window": 2,
        "pump_dump_pct": 1.0,
        "vol_spike_window": 2,
        "vol_spike_multiplier": 1.0,
        "high_volatility_pct": 0.0001,
        "signal_min_confidence": 0,
        "regime_refresh_interval_seconds": 10_000.0,
        # Offline replay/validation harness: independent of the live product's
        # enable_signals gate (30-day Market Intelligence evaluation, frozen
        # 2026-06-12) -- this harness exercises the signal-emission pipeline
        # directly.
        "enable_signals": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _row(ts_ms: int, o: float, h: float, low: float, c: float, v: float) -> dict:
    return {
        "symbol_id": 1,
        "ts": datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
        "interval": "1m",
        "o": o,
        "h": h,
        "l": low,
        "c": c,
        "v": v,
        "quote_v": v * c,
        "trades": 1,
        "taker_buy_v": 0.0,
    }


_ROWS = [
    _row(0, 100, 101, 99, 100, 10),
    _row(60_000, 100, 103, 99, 103, 12),
    _row(120_000, 103, 108, 102, 108, 15),
    _row(180_000, 108, 120, 107, 120, 50),
    _row(240_000, 120, 130, 119, 128, 60),
]


def _pool() -> FakePool:
    pool = FakePool()
    pool.fetchval_return = 1  # symbols.id for "BTC"
    pool.fetch_return = _ROWS
    pool.fetch_routes["FROM symbols"] = [{"base": "BTC", "tags": []}]
    return pool


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


async def test_run_policy_validation_is_additive_over_regime_validation() -> None:
    base = await run_regime_validation(_pool(), _settings(), symbols=["BTC"])
    full = await run_policy_validation(_pool(), _settings(), symbols=["BTC"])

    for key in ("signals", "metrics", "dataset_type", "mode", "regime_metrics"):
        assert _to_jsonable(full[key]) == _to_jsonable(base[key])

    assert set(full.keys()) == {
        "signals",
        "metrics",
        "dataset_type",
        "mode",
        "regime_metrics",
        "policy_decisions",
    }
    assert set(full["policy_decisions"].keys()) == {"BTC"}


async def test_run_policy_validation_unknown_symbol_is_empty() -> None:
    pool = FakePool()
    pool.fetchval_return = None  # symbols.get_id -> None
    pool.fetch_return = _ROWS
    pool.fetch_routes["FROM symbols"] = [{"base": "BTC", "tags": []}]

    result = await run_policy_validation(pool, _settings(), symbols=["NOPE"])

    assert result["regime_metrics"]["NOPE"] == {}
    assert result["policy_decisions"]["NOPE"] == {}


async def test_run_policy_validation_is_deterministic() -> None:
    result_a = await run_policy_validation(_pool(), _settings(), symbols=["BTC"])
    result_b = await run_policy_validation(_pool(), _settings(), symbols=["BTC"])

    json_a = json.dumps(_to_jsonable(result_a), sort_keys=True)
    json_b = json.dumps(_to_jsonable(result_b), sort_keys=True)
    assert json_a == json_b


async def test_policy_decisions_shape_and_values() -> None:
    result = await run_policy_validation(_pool(), _settings(), symbols=["BTC"])

    decisions = result["policy_decisions"]["BTC"]
    assert decisions  # at least one bucket from the guaranteed signal

    for bucket_key, by_type in decisions.items():
        assert bucket_key in result["regime_metrics"]["BTC"]
        for sig_type, cell in by_type.items():
            assert set(cell.keys()) == {
                "decision",
                "reason",
                "regime_classification",
                "regime_edge",
                "global_edge",
                "sample_size",
                "neff",
            }
            assert cell["decision"] in (ALLOW, BLOCK, REDUCE)
            regime_cell = result["regime_metrics"]["BTC"][bucket_key][sig_type]
            assert cell["regime_classification"] == regime_cell["classification"][1]
            assert cell["regime_edge"] == regime_cell["edge"][1]
            assert cell["sample_size"] == regime_cell["sample_size"]
            assert cell["neff"] == regime_cell["neff"][1]


def test_compute_policy_decisions_insufficient_data_blocks() -> None:
    result = {
        "metrics": {"edge": {"buy": {1: 0.01}}},
        "regime_metrics": {
            "BTC": {
                "trend=up,vol=normal": {
                    "buy": {
                        "sample_size": 1,
                        "clustered_fraction": 0.0,
                        "neff": {1: 1.0},
                        "edge": {1: 0.01},
                        "edge_ci_low": {1: None},
                        "edge_ci_high": {1: None},
                        "classification": {1: "insufficient_data"},
                    }
                }
            }
        },
    }

    decisions = compute_policy_decisions(result, decision_horizon=1)
    cell = decisions["BTC"]["trend=up,vol=normal"]["buy"]
    assert cell["decision"] == BLOCK
    assert "insufficient" in cell["reason"]


def test_compute_policy_decisions_diagnostic_only_ci_spans_zero_reduces() -> None:
    result = {
        "metrics": {"edge": {"buy": {1: 0.01}}},
        "regime_metrics": {
            "BTC": {
                "trend=up,vol=normal": {
                    "buy": {
                        "sample_size": 10,
                        "clustered_fraction": 0.0,
                        "neff": {1: 6.0},
                        "edge": {1: 0.005},
                        "edge_ci_low": {1: -0.01},
                        "edge_ci_high": {1: 0.02},
                        "classification": {1: "diagnostic_only"},
                    }
                }
            }
        },
    }

    decisions = compute_policy_decisions(result, decision_horizon=1)
    cell = decisions["BTC"]["trend=up,vol=normal"]["buy"]
    assert cell["decision"] == REDUCE
    assert "CI includes zero" in cell["reason"]


def test_compute_policy_decisions_diagnostic_only_clustered_reduces() -> None:
    result = {
        "metrics": {"edge": {"buy": {1: 0.01}}},
        "regime_metrics": {
            "BTC": {
                "trend=up,vol=normal": {
                    "buy": {
                        "sample_size": 10,
                        "clustered_fraction": 0.9,
                        "neff": {1: 6.0},
                        "edge": {1: 0.005},
                        "edge_ci_low": {1: 0.001},
                        "edge_ci_high": {1: 0.02},
                        "classification": {1: "diagnostic_only"},
                    }
                }
            }
        },
    }

    decisions = compute_policy_decisions(result, decision_horizon=1)
    cell = decisions["BTC"]["trend=up,vol=normal"]["buy"]
    assert cell["decision"] == REDUCE
    assert "clustered" in cell["reason"]


def test_compute_policy_decisions_candidate_agrees_with_global_allows() -> None:
    result = {
        "metrics": {"edge": {"buy": {1: 0.02}}},
        "regime_metrics": {
            "BTC": {
                "trend=up,vol=normal": {
                    "buy": {
                        "sample_size": 10,
                        "clustered_fraction": 0.1,
                        "neff": {1: 6.0},
                        "edge": {1: 0.015},
                        "edge_ci_low": {1: 0.001},
                        "edge_ci_high": {1: 0.03},
                        "classification": {1: "regime_conditional_candidate"},
                    }
                }
            }
        },
    }

    decisions = compute_policy_decisions(result, decision_horizon=1)
    cell = decisions["BTC"]["trend=up,vol=normal"]["buy"]
    assert cell["decision"] == ALLOW
    assert "agrees" in cell["reason"]


def test_compute_policy_decisions_candidate_diverges_from_global_reduces() -> None:
    result = {
        "metrics": {"edge": {"buy": {1: -0.02}}},
        "regime_metrics": {
            "BTC": {
                "trend=up,vol=normal": {
                    "buy": {
                        "sample_size": 10,
                        "clustered_fraction": 0.1,
                        "neff": {1: 6.0},
                        "edge": {1: 0.015},
                        "edge_ci_low": {1: 0.001},
                        "edge_ci_high": {1: 0.03},
                        "classification": {1: "regime_conditional_candidate"},
                    }
                }
            }
        },
    }

    decisions = compute_policy_decisions(result, decision_horizon=1)
    cell = decisions["BTC"]["trend=up,vol=normal"]["buy"]
    assert cell["decision"] == REDUCE
    assert "diverges" in cell["reason"]


def test_compute_policy_decisions_candidate_no_global_reference_reduces() -> None:
    result = {
        "metrics": {"edge": {}},
        "regime_metrics": {
            "BTC": {
                "trend=up,vol=normal": {
                    "buy": {
                        "sample_size": 10,
                        "clustered_fraction": 0.1,
                        "neff": {1: 6.0},
                        "edge": {1: 0.015},
                        "edge_ci_low": {1: 0.001},
                        "edge_ci_high": {1: 0.03},
                        "classification": {1: "regime_conditional_candidate"},
                    }
                }
            }
        },
    }

    decisions = compute_policy_decisions(result, decision_horizon=1)
    cell = decisions["BTC"]["trend=up,vol=normal"]["buy"]
    assert cell["decision"] == REDUCE
    assert "no global edge reference" in cell["reason"]
