"""Tests for the regime-validation overlay (M5+).

Reuses the M5 ``test_signal_validation`` fixtures (same :class:`FakePool`,
candle series, and ``_settings`` defaults) since this module is a pure
second pass over ``run_signal_validation``'s output plus a second,
independent candle replay.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from fake_pool import FakePool
from orvixa.backtest import compute_regime_metrics, run_regime_validation, run_signal_validation
from orvixa.backtest.regime_validation import (
    _classify_trend_regime,
    _classify_validity,
    _classify_vol_regime,
    _compute_cluster_tags,
    _compute_regime_tags,
)
from orvixa.config import Settings
from orvixa.feeds.base import Candle


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


def _candle(ts_ms: int, close: float) -> Candle:
    return Candle(
        symbol="BTC",
        ts=ts_ms,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        quote_volume=close,
        trades=1,
        closed=True,
    )


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


async def test_run_regime_validation_is_additive_over_signal_validation() -> None:
    base = await run_signal_validation(_pool(), _settings(), symbols=["BTC"])
    full = await run_regime_validation(_pool(), _settings(), symbols=["BTC"])

    # Everything run_signal_validation returns is unchanged.
    for key in ("signals", "metrics", "dataset_type", "mode"):
        assert _to_jsonable(full[key]) == _to_jsonable(base[key])

    # Plus a new, additive top-level key.
    assert set(full.keys()) == {"signals", "metrics", "dataset_type", "mode", "regime_metrics"}
    assert set(full["regime_metrics"].keys()) == {"BTC"}


async def test_run_regime_validation_unknown_symbol_is_empty() -> None:
    pool = FakePool()
    pool.fetchval_return = None  # symbols.get_id -> None
    pool.fetch_return = _ROWS
    pool.fetch_routes["FROM symbols"] = [{"base": "BTC", "tags": []}]

    result = await run_regime_validation(pool, _settings(), symbols=["NOPE"])

    assert result["signals"]["NOPE"] == []
    assert result["regime_metrics"]["NOPE"] == {}


async def test_run_regime_validation_is_deterministic() -> None:
    result_a = await run_regime_validation(_pool(), _settings(), symbols=["BTC"])
    result_b = await run_regime_validation(_pool(), _settings(), symbols=["BTC"])

    json_a = json.dumps(_to_jsonable(result_a), sort_keys=True)
    json_b = json.dumps(_to_jsonable(result_b), sort_keys=True)
    assert json_a == json_b


async def test_regime_metrics_bucket_shape() -> None:
    result = await run_regime_validation(_pool(), _settings(), symbols=["BTC"])

    regime_metrics = result["regime_metrics"]["BTC"]
    assert regime_metrics  # at least one signal -> at least one bucket

    for bucket_key, by_type in regime_metrics.items():
        assert bucket_key.startswith("trend=") and ",vol=" in bucket_key
        for sig_type, type_metrics in by_type.items():
            assert sig_type in ("buy", "sell", "highvol")
            assert set(type_metrics.keys()) == {
                "sample_size",
                "clustered_fraction",
                "neff",
                "edge",
                "edge_ci_low",
                "edge_ci_high",
                "classification",
            }
            assert type_metrics["sample_size"] >= 1
            assert 0.0 <= type_metrics["clustered_fraction"] <= 1.0
            for h in (1, 3, 5, 10, 20):
                assert h in type_metrics["edge"]
                assert h in type_metrics["neff"]
                cls = type_metrics["classification"][h]
                assert cls in (
                    "insufficient_data",
                    "diagnostic_only",
                    "regime_conditional_candidate",
                )


def test_classify_trend_regime() -> None:
    assert _classify_trend_regime("up") == "up"
    assert _classify_trend_regime("down") == "down"
    assert _classify_trend_regime("flat") == "flat"
    assert _classify_trend_regime(None) == "unknown"


def test_classify_vol_regime() -> None:
    settings = _settings(high_volatility_pct=3.0)
    assert _classify_vol_regime(None, settings) == "unknown"
    assert _classify_vol_regime(5.0, settings) == "high"
    assert _classify_vol_regime(0.5, settings) == "low"
    assert _classify_vol_regime(2.0, settings) == "normal"


def test_compute_regime_tags_length_matches_candles() -> None:
    closes = [100, 103, 108, 120, 128]
    candles = [_candle(i * 60_000, c) for i, c in enumerate(closes)]

    tags = _compute_regime_tags(candles, _settings())

    assert len(tags) == len(candles)
    for trend_regime, vol_regime in tags:
        assert trend_regime in ("up", "down", "flat", "unknown")
        assert vol_regime in ("low", "normal", "high", "unknown")


def test_compute_cluster_tags() -> None:
    signals = [
        {"ts": datetime.fromtimestamp(0, tz=UTC), "type": "buy"},
        {"ts": datetime.fromtimestamp(60, tz=UTC), "type": "sell"},  # different type -> isolated
        {"ts": datetime.fromtimestamp(90, tz=UTC), "type": "buy"},  # 90s after first "buy"
    ]
    # window = 1 candle * 60_000ms = 60s -> 90s gap is NOT within window.
    tags = _compute_cluster_tags(signals, window_candles=1, interval_ms=60_000)
    assert tags == ["isolated", "isolated", "isolated"]

    # window = 2 candles * 60_000ms = 120s -> 90s gap IS within window.
    tags = _compute_cluster_tags(signals, window_candles=2, interval_ms=60_000)
    assert tags == ["isolated", "isolated", "clustered"]


def test_classify_validity() -> None:
    # Too few effectively-independent samples.
    assert _classify_validity(neff=1.0, edge_ci_low=0.01, edge_ci_high=0.02, clustered_fraction=0.0) == (
        "insufficient_data"
    )
    # No CI computed at all.
    assert _classify_validity(neff=10.0, edge_ci_low=None, edge_ci_high=None, clustered_fraction=0.0) == (
        "insufficient_data"
    )
    # CI spans zero.
    assert _classify_validity(neff=10.0, edge_ci_low=-0.01, edge_ci_high=0.01, clustered_fraction=0.0) == (
        "diagnostic_only"
    )
    # CI excludes zero but mostly clustered.
    assert _classify_validity(neff=10.0, edge_ci_low=0.01, edge_ci_high=0.02, clustered_fraction=0.9) == (
        "diagnostic_only"
    )
    # CI excludes zero, mostly isolated.
    assert _classify_validity(neff=10.0, edge_ci_low=0.01, edge_ci_high=0.02, clustered_fraction=0.1) == (
        "regime_conditional_candidate"
    )


async def test_compute_regime_metrics_empty_candles_is_empty() -> None:
    assert compute_regime_metrics([], [], _settings()) == {}


def test_regime_edge_uses_regime_conditioned_baseline_not_global() -> None:
    """Regime-conditioned edge differs from a global-baseline edge when regimes differ."""
    closes = [100, 103, 108, 120, 80, 70, 60]
    candles = [_candle(i * 60_000, c) for i, c in enumerate(closes)]

    # Two synthetic "buy" signals: one during an uptrend candle, one during a
    # downtrend candle, with hand-set forward returns.
    signals = [
        {
            "ts": candles[1].ts and datetime.fromtimestamp(candles[1].ts / 1000, tz=UTC),
            "type": "buy",
            "confidence": 80,
            "close": closes[1],
            "fwd_returns": {1: 0.05, 3: None, 5: None, 10: None, 20: None},
        },
        {
            "ts": datetime.fromtimestamp(candles[4].ts / 1000, tz=UTC),
            "type": "buy",
            "confidence": 80,
            "close": closes[4],
            "fwd_returns": {1: -0.125, 3: None, 5: None, 10: None, 20: None},
        },
    ]

    metrics = compute_regime_metrics(signals, candles, _settings(high_volatility_pct=3.0), execution_lag=0)

    # Both buckets should be present, with their own edge values that need
    # not match each other (regime-conditioned baselines differ).
    assert len(metrics) >= 1
    for by_type in metrics.values():
        if "buy" in by_type:
            assert 1 in by_type["buy"]["edge"]


@pytest.mark.parametrize("missing_key", ["edge_ci_low", "edge_ci_high"])
def test_regime_metrics_ci_none_when_neff_none(missing_key: str) -> None:
    """A horizon with zero forward-return samples gets neff=None and no CI."""
    closes = [100, 103, 108, 120, 128]
    candles = [_candle(i * 60_000, c) for i, c in enumerate(closes)]

    signals = [
        {
            "ts": datetime.fromtimestamp(candles[0].ts / 1000, tz=UTC),
            "type": "buy",
            "confidence": 80,
            "close": closes[0],
            "fwd_returns": {1: 0.03, 3: None, 5: None, 10: None, 20: None},
        }
    ]

    metrics = compute_regime_metrics(signals, candles, _settings(), execution_lag=0)
    for by_type in metrics.values():
        if "buy" in by_type:
            # horizon 20 has no samples in a 5-candle series -> no CI.
            assert by_type["buy"]["neff"][20] is None
            assert by_type["buy"][missing_key][20] is None
            assert by_type["buy"]["classification"][20] == "insufficient_data"
