"""Tests for the M5 minimal signal-validation harness.

Fully fake-driven -- :class:`fake_pool.FakePool` stands in for ``candles`` /
``symbols`` (no live database), mirroring the M4 ``test_analytics_engine``
pattern. The candle series is the same one M4 uses to guarantee at least one
HIGH VOLATILITY signal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from fake_pool import FakePool
from orvixa.backtest import (
    EDGE_EVALUATION,
    PIPELINE_CORRECTNESS,
    REAL,
    SYNTHETIC,
    compute_signal_metrics,
    run_signal_validation,
)
from orvixa.backtest.dataset import classify_dataset
from orvixa.backtest.signal_validation import (
    _autocorr,
    _baseline_returns,
    _bootstrap_ci,
    _effective_sample_size,
    _forward_returns,
    _interval_to_ms,
    _percentile,
)
from orvixa.config import Settings
from orvixa.db.repository import SymbolRepository
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


def _pool(*, synthetic: bool = False) -> FakePool:
    pool = FakePool()
    pool.fetchval_return = 1  # symbols.id for "BTC"
    pool.fetch_return = _ROWS
    tags = ["synthetic_data"] if synthetic else []
    pool.fetch_routes["FROM symbols"] = [{"base": "BTC", "tags": tags}]
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


async def test_run_signal_validation_per_symbol_structure() -> None:
    result = await run_signal_validation(_pool(), _settings(), symbols=["BTC"])

    assert set(result.keys()) == {"signals", "metrics", "dataset_type", "mode"}
    assert result["dataset_type"] == REAL
    assert result["mode"] == PIPELINE_CORRECTNESS
    assert set(result["signals"].keys()) == {"BTC"}

    btc_signals = result["signals"]["BTC"]
    assert len(btc_signals) >= 1  # high_volatility_pct=0.0001 -> guaranteed signal

    for entry in btc_signals:
        assert set(entry.keys()) == {"ts", "type", "confidence", "close", "fwd_returns"}
        assert entry["type"] in ("buy", "sell", "highvol")
        assert set(entry["fwd_returns"].keys()) == {1, 3, 5, 10, 20}

    metrics = result["metrics"]
    assert set(metrics.keys()) == {
        "sample_size",
        "baseline_return",
        "baseline_neff",
        "mean_return",
        "mean_return_ci_low",
        "mean_return_ci_high",
        "edge",
        "edge_ci_low",
        "edge_ci_high",
        "neff",
        "hit_rate",
    }


async def test_run_signal_validation_unknown_symbol_is_empty() -> None:
    pool = FakePool()
    pool.fetchval_return = None  # symbols.get_id -> None
    pool.fetch_return = _ROWS
    pool.fetch_routes["FROM symbols"] = [{"base": "BTC", "tags": []}]

    result = await run_signal_validation(pool, _settings(), symbols=["NOPE"])

    assert result["signals"]["NOPE"] == []
    assert result["metrics"]["sample_size"] == {}


async def test_run_signal_validation_is_deterministic() -> None:
    """Re-running with the same input must produce byte-identical output."""
    result_a = await run_signal_validation(_pool(), _settings(), symbols=["BTC"])
    result_b = await run_signal_validation(_pool(), _settings(), symbols=["BTC"])

    json_a = json.dumps(_to_jsonable(result_a), sort_keys=True)
    json_b = json.dumps(_to_jsonable(result_b), sort_keys=True)
    assert json_a == json_b


async def test_forward_returns_use_default_execution_lag() -> None:
    """Forward returns are measured from candles[idx + DEFAULT_EXECUTION_LAG], not candles[idx]."""
    result = await run_signal_validation(_pool(), _settings(), symbols=["BTC"])
    closes = [r["c"] for r in _ROWS]

    for entry in result["signals"]["BTC"]:
        idx = closes.index(entry["close"])
        entry_idx = idx + 1  # DEFAULT_EXECUTION_LAG
        for h, fwd in entry["fwd_returns"].items():
            target = entry_idx + h
            if entry_idx >= len(closes) or target >= len(closes):
                assert fwd is None
            else:
                assert fwd == closes[target] / closes[entry_idx] - 1.0


def test_forward_returns_execution_lag_shifts_entry_point() -> None:
    closes = [100, 103, 108, 120, 128]
    candles = [_candle(i * 60_000, c) for i, c in enumerate(closes)]

    # No lag (legacy behaviour): horizon-1 from idx=0 is close[1]/close[0]-1.
    no_lag = _forward_returns(candles, idx=0, horizons=(1,), execution_lag=0)
    assert no_lag[1] == closes[1] / closes[0] - 1.0

    # Default lag of 1: entry happens one candle later, at idx=1.
    lagged = _forward_returns(candles, idx=0, horizons=(1,), execution_lag=1)
    assert lagged[1] == closes[2] / closes[1] - 1.0

    # Entry point beyond the series -> every horizon is None.
    tail = _forward_returns(candles, idx=4, horizons=(1, 5), execution_lag=1)
    assert tail == {1: None, 5: None}


def test_forward_returns_horizon_is_time_based_not_index_based() -> None:
    """A gap in the series shifts the *index* a horizon resolves to, not its meaning.

    Horizon h means "h * interval_ms after the entry candle's timestamp",
    resolved to the first candle at or after that time -- not "h candles
    after the entry candle".
    """
    # 1m interval. Entry candle at t=0. A 3-minute gap follows the second
    # candle (t=60_000 -> t=240_000), then candles resume on the minute.
    timestamps = [0, 60_000, 240_000, 300_000, 360_000]
    closes = [100, 103, 110, 112, 115]
    candles = [_candle(ts, c) for ts, c in zip(timestamps, closes, strict=True)]

    result = _forward_returns(candles, idx=0, horizons=(1, 3), execution_lag=0)

    # horizon=1 (1 minute): target=60_000 lands exactly on idx=1.
    assert result[1] == closes[1] / closes[0] - 1.0

    # horizon=3 (3 minutes): target=180_000, which doesn't exist. The first
    # candle at/after that time is idx=2 (t=240_000) -- NOT idx=3, which is
    # what the old "3 candles ahead" definition would have used.
    assert result[3] == closes[2] / closes[0] - 1.0
    assert result[3] != closes[3] / closes[0] - 1.0


def test_interval_to_ms() -> None:
    assert _interval_to_ms("1m") == 60_000
    assert _interval_to_ms("5m") == 5 * 60_000
    assert _interval_to_ms("1h") == 3_600_000
    assert _interval_to_ms("1d") == 86_400_000

    with pytest.raises(ValueError, match="unsupported interval"):
        _interval_to_ms("bogus")


def test_baseline_returns_match_forward_returns_at_every_index() -> None:
    """_baseline_returns is just _forward_returns pooled across every index."""
    closes = [100, 103, 108, 120, 128]
    candles = [_candle(i * 60_000, c) for i, c in enumerate(closes)]
    horizons = (1, 3, 5, 10, 20)

    for execution_lag in (0, 1):
        baseline = _baseline_returns(candles, horizons, execution_lag)

        expected: dict[int, list[float]] = {h: [] for h in horizons}
        for idx in range(len(candles)):
            for h, value in _forward_returns(candles, idx, horizons, execution_lag).items():
                if value is not None:
                    expected[h].append(value)

        assert baseline == expected
        assert baseline[20] == []  # no pair 20 candles apart in a 5-candle series


def test_compute_signal_metrics_baseline_and_edge() -> None:
    closes = [100, 103, 108, 120, 128]
    candles_btc = [_candle(i * 60_000, c) for i, c in enumerate(closes)]

    signals = {
        "BTC": [
            {
                "ts": datetime.fromtimestamp(0, tz=UTC),
                "type": "buy",
                "confidence": 80,
                "close": 100,
                "fwd_returns": {1: 0.03, 3: 0.20, 5: None, 10: None, 20: None},
            },
            {
                "ts": datetime.fromtimestamp(60, tz=UTC),
                "type": "sell",
                "confidence": 70,
                "close": 103,
                "fwd_returns": {1: 0.0485, 3: None, 5: None, 10: None, 20: None},
            },
        ]
    }
    candles_by_symbol = {"BTC": candles_btc}

    # execution_lag=0 reproduces the un-shifted baseline this test hand-computes below.
    metrics = compute_signal_metrics(
        signals, candles_by_symbol, horizons=(1, 3, 5, 10, 20), execution_lag=0
    )

    assert metrics["sample_size"] == {"buy": 1, "sell": 1}

    # baseline_return[1] is the mean of (103/100-1, 108/103-1, 120/108-1, 128/120-1)
    expected_baseline_1 = sum(
        closes[i + 1] / closes[i] - 1.0 for i in range(len(closes) - 1)
    ) / (len(closes) - 1)
    assert metrics["baseline_return"][1] == expected_baseline_1
    assert metrics["baseline_return"][20] is None  # no pair 20 apart in 5 candles

    assert metrics["mean_return"]["buy"][1] == 0.03
    assert metrics["mean_return"]["buy"][5] is None
    assert metrics["edge"]["buy"][1] == 0.03 - expected_baseline_1
    assert metrics["edge"]["buy"][5] is None

    # buy hit_rate[1]: fwd_return 0.03 > 0 -> 1.0
    assert metrics["hit_rate"]["buy"][1] == 1.0
    # sell hit_rate[1]: fwd_return 0.0485 > 0 -> not a "sell win" -> 0.0
    assert metrics["hit_rate"]["sell"][1] == 0.0
    # no horizon-3 sell sample -> None
    assert metrics["hit_rate"]["sell"][3] is None

    # neff: a single-sample series has neff == 1 == sample_size.
    assert metrics["neff"]["buy"][1] == 1.0
    assert metrics["neff"]["buy"][1] <= metrics["sample_size"]["buy"]
    # no horizon-5 sample for "buy" -> neff is None.
    assert metrics["neff"]["buy"][5] is None

    # baseline_neff is reported per horizon, derived from the pooled baseline.
    assert metrics["baseline_neff"][1] is not None
    assert metrics["baseline_neff"][1] <= len(baseline_pool_for(candles_btc, 1))
    assert metrics["baseline_neff"][20] is None  # empty baseline pool at horizon 20

    # CI bounds bracket the point estimates wherever both are defined.
    for sig_type in ("buy", "sell"):
        for h in (1, 3, 5, 10, 20):
            mean = metrics["mean_return"][sig_type][h]
            lo = metrics["mean_return_ci_low"][sig_type][h]
            hi = metrics["mean_return_ci_high"][sig_type][h]
            if mean is None:
                assert lo is None
                assert hi is None
            else:
                assert lo <= mean <= hi

            edge_val = metrics["edge"][sig_type][h]
            e_lo = metrics["edge_ci_low"][sig_type][h]
            e_hi = metrics["edge_ci_high"][sig_type][h]
            if edge_val is None:
                assert e_lo is None
                assert e_hi is None
            else:
                assert e_lo <= edge_val <= e_hi


def baseline_pool_for(candles: list[Candle], horizon: int) -> list[float]:
    pool: list[float] = []
    for idx in range(len(candles)):
        value = _forward_returns(candles, idx, (horizon,), execution_lag=0)[horizon]
        if value is not None:
            pool.append(value)
    return pool


def test_autocorr_basic() -> None:
    # Constant series has zero variance -> autocorr is 0.
    assert _autocorr([1.0, 1.0, 1.0], 1) == 0.0
    # Out-of-range lags are 0.
    assert _autocorr([1.0, 2.0, 3.0], 0) == 0.0
    assert _autocorr([1.0, 2.0, 3.0], 3) == 0.0
    # A monotonic (trending) series has positive autocorrelation at lag 1.
    assert _autocorr([1.0, 2.0, 3.0, 4.0, 5.0], 1) > 0.0


def test_effective_sample_size() -> None:
    assert _effective_sample_size([], max_lag=5) is None
    assert _effective_sample_size([1.0], max_lag=5) == 1.0
    # max_lag <= 0 -> no adjustment.
    assert _effective_sample_size([1.0, 2.0, 3.0], max_lag=0) == 3.0

    # Highly autocorrelated (overlapping-style) series -> neff < N.
    values = [float(i) for i in range(20)]
    neff = _effective_sample_size(values, max_lag=4)
    assert neff is not None
    assert 1.0 <= neff <= 20.0
    assert neff < 20.0


def test_percentile() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _percentile(values, 0.0) == 1.0
    assert _percentile(values, 0.99) == 5.0


def test_bootstrap_ci_is_deterministic() -> None:
    signal_returns = [0.01, 0.02, -0.01, 0.03, 0.0]
    baseline_returns = [0.0, 0.005, -0.005, 0.01, -0.01]

    result_a = _bootstrap_ci(signal_returns, baseline_returns, 5.0, 5.0, iterations=200, seed=42)
    result_b = _bootstrap_ci(signal_returns, baseline_returns, 5.0, 5.0, iterations=200, seed=42)
    assert result_a == result_b

    (mean_lo, mean_hi), (edge_lo, edge_hi) = result_a
    assert mean_lo <= mean_hi
    assert edge_lo <= edge_hi


async def test_classify_dataset_real_vs_synthetic() -> None:
    real_pool = FakePool()
    real_pool.fetch_routes["FROM symbols"] = [{"base": "BTC", "tags": ["core"]}]
    assert await classify_dataset(SymbolRepository(real_pool), ["BTC"]) == REAL

    synthetic_pool = FakePool()
    synthetic_pool.fetch_routes["FROM symbols"] = [{"base": "BTC", "tags": ["synthetic_data"]}]
    assert await classify_dataset(SymbolRepository(synthetic_pool), ["BTC"]) == SYNTHETIC


async def test_run_signal_validation_reports_real_dataset() -> None:
    result = await run_signal_validation(_pool(synthetic=False), _settings(), symbols=["BTC"])
    assert result["dataset_type"] == REAL
    assert result["mode"] == PIPELINE_CORRECTNESS


async def test_run_signal_validation_synthetic_pipeline_correctness_warns(
    capsys: Any,
) -> None:
    result = await run_signal_validation(
        _pool(synthetic=True), _settings(), symbols=["BTC"], mode=PIPELINE_CORRECTNESS
    )
    assert result["dataset_type"] == SYNTHETIC
    assert result["mode"] == PIPELINE_CORRECTNESS

    out = capsys.readouterr().out
    assert "dataset: SYNTHETIC" in out
    assert "WARNING: dataset is SYNTHETIC" in out


async def test_run_signal_validation_synthetic_edge_evaluation_raises() -> None:
    with pytest.raises(ValueError, match="SYNTHETIC"):
        await run_signal_validation(
            _pool(synthetic=True), _settings(), symbols=["BTC"], mode=EDGE_EVALUATION
        )


async def test_run_signal_validation_invalid_mode_raises() -> None:
    with pytest.raises(ValueError, match="invalid mode"):
        await run_signal_validation(_pool(), _settings(), symbols=["BTC"], mode="bogus")
