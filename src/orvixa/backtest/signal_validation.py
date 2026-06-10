"""Replay candles through ``AnalyticsEngine`` and score the signals it emits.

For each symbol, independently:

1. load that symbol's candle history from ``candles`` (ascending by ``ts``);
2. construct a fresh :class:`~orvixa.analytics.engine.AnalyticsEngine`;
3. feed it the candles one at a time, in order, via ``handle_candle``;
4. record every :class:`~orvixa.db.models.SignalRow` it emits, together with
   the forward returns of the underlying close price at ``DEFAULT_HORIZONS``.

:func:`compute_signal_metrics` then turns the captured signals into hit
rates and an "edge" over the symbol's own buy-every-candle baseline.
Forward returns and the baseline are both computed by the same pair of pure
helpers (:func:`_forward_returns` / :func:`_baseline_returns`), the single
source of truth for both calculations, using a shared ``execution_lag``.
Horizons are wall-clock durations (``h * interval_ms`` past the entry
candle's timestamp), resolved to the first candle at or after that target
time -- so results stay time-consistent across gaps in the candle series,
not just under the gap-free synthetic dataset.

Given the same candle rows and settings, replay is deterministic: re-running
:func:`run_signal_validation` produces byte-identical output. Nothing here
touches indicator, event, or regime logic, and no symbol's data ever
influences another symbol's replay.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from ..analytics.engine import AnalyticsEngine
from ..config import Settings
from ..db.repository import CandleRepository, SymbolRepository
from ..feeds.base import Candle
from .capture import NullSink, SignalCaptureSink
from .dataset import SYNTHETIC, classify_dataset

DEFAULT_HORIZONS: tuple[int, ...] = (1, 3, 5, 10, 20)

# Validation modes: "pipeline_correctness" exercises the replay/metrics
# pipeline without claiming any predictive edge; "edge_evaluation" asserts
# the results are evidence of real signal edge and is refused on a
# SYNTHETIC dataset (see .dataset.classify_dataset).
PIPELINE_CORRECTNESS: str = "pipeline_correctness"
EDGE_EVALUATION: str = "edge_evaluation"
VALID_MODES: tuple[str, ...] = (PIPELINE_CORRECTNESS, EDGE_EVALUATION)

# Candles between a signal firing and "execution": forward returns are
# measured from candles[i + EXECUTION_LAG] rather than candles[i], so a
# horizon-1 return reflects close[i+2]/close[i+1] (entering one candle after
# the signal, not on its own close) -- removing same-candle/next-candle
# look-ahead bias. The baseline uses the same shift for a like-for-like
# comparison.
DEFAULT_EXECUTION_LAG: int = 1

# Default candle interval (1 minute) in milliseconds. Each horizon in
# DEFAULT_HORIZONS is a multiple of this duration -- e.g. horizon=20 with a
# 1m interval means "20 minutes after entry", not "20 candles after entry".
# See _forward_returns / _interval_to_ms.
DEFAULT_INTERVAL_MS: int = 60_000

_INTERVAL_UNIT_MS: dict[str, int] = {
    "s": 1_000,
    "m": 60_000,
    "h": 3_600_000,
    "d": 86_400_000,
}


def _interval_to_ms(interval: str) -> int:
    """Parse a candle interval string (e.g. ``"1m"``, ``"5m"``, ``"1h"``) to milliseconds.

    Used to convert horizon counts into wall-clock durations for
    timestamp-based forward returns (see :func:`_forward_returns`).
    """
    unit = interval[-1].lower()
    if unit not in _INTERVAL_UNIT_MS or not interval[:-1].isdigit():
        raise ValueError(f"unsupported interval format: {interval!r}")
    return int(interval[:-1]) * _INTERVAL_UNIT_MS[unit]


def _row_to_candle(symbol: str, row: Any) -> Candle:
    """Map a ``select_range`` record to a closed :class:`~orvixa.feeds.base.Candle`."""
    return Candle(
        symbol=symbol,
        ts=int(row["ts"].timestamp() * 1000),
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row["v"]),
        quote_volume=float(row["quote_v"]),
        trades=int(row["trades"]),
        closed=True,
        taker_buy_volume=float(row["taker_buy_v"]),
    )


def _forward_returns(
    candles: Sequence[Candle],
    idx: int,
    horizons: Sequence[int],
    execution_lag: int = DEFAULT_EXECUTION_LAG,
    interval_ms: int = DEFAULT_INTERVAL_MS,
) -> dict[int, float | None]:
    """Forward returns from the execution candle ``idx + execution_lag``.

    Each horizon ``h`` is a *duration* of ``h * interval_ms`` past the entry
    candle's timestamp -- not "h candles ahead" -- so results stay
    time-consistent across gaps in the candle series. The return for a given
    ``h`` is taken from the first candle at or after that target time; if no
    such candle exists (the target time is beyond the series), the horizon
    is ``None``.

    The single source of truth for "forward return" -- both per-signal
    returns and the baseline (:func:`_baseline_returns`) are built on this.
    """
    entry_idx = idx + execution_lag
    if entry_idx >= len(candles):
        return dict.fromkeys(horizons)

    entry_close = candles[entry_idx].close
    entry_ts = candles[entry_idx].ts
    out: dict[int, float | None] = {}
    for h in horizons:
        target_time = entry_ts + h * interval_ms
        target = entry_idx
        while target < len(candles) and candles[target].ts < target_time:
            target += 1
        if target >= len(candles) or entry_close == 0:
            out[h] = None
        else:
            out[h] = candles[target].close / entry_close - 1.0
    return out


def _baseline_returns(
    candles: Sequence[Candle],
    horizons: Sequence[int],
    execution_lag: int = DEFAULT_EXECUTION_LAG,
    interval_ms: int = DEFAULT_INTERVAL_MS,
) -> dict[int, list[float]]:
    """Per-candle forward returns across the whole series -- the "do nothing" reference.

    Built from :func:`_forward_returns` at every index, so the baseline uses
    exactly the same execution-lag-shifted entry point and time-based
    horizons as signal returns.
    """
    out: dict[int, list[float]] = {h: [] for h in horizons}
    for idx in range(len(candles)):
        for h, value in _forward_returns(candles, idx, horizons, execution_lag, interval_ms).items():
            if value is not None:
                out[h].append(value)
    return out


def _autocorr(values: Sequence[float], lag: int) -> float:
    """Sample autocorrelation of ``values`` at ``lag`` (lag-k Pearson correlation)."""
    n = len(values)
    if lag <= 0 or lag >= n:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values)
    if var == 0:
        return 0.0
    cov = sum((values[i] - mean) * (values[i + lag] - mean) for i in range(n - lag))
    return cov / var


def _effective_sample_size(values: Sequence[float], max_lag: int) -> float | None:
    """Autocorrelation-adjusted sample size: ``N / (1 + 2 * sum(autocorr(k)))``.

    ``max_lag`` is ``horizon - 1``: h-period overlapping forward returns
    follow an MA(h-1) process, so autocorrelation from window overlap alone
    is not expected beyond lag ``h-1``. The result is clamped to ``[1, N]``
    -- Neff can shrink the raw count to reflect overlap, but never exceed it
    or reach zero.
    """
    n = len(values)
    if n == 0:
        return None
    if n == 1 or max_lag <= 0:
        return float(n)
    autocorr_sum = sum(_autocorr(values, k) for k in range(1, min(max_lag, n - 1) + 1))
    denom = max(1.0 + 2.0 * autocorr_sum, 1e-9)
    return min(max(n / denom, 1.0), float(n))


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (``0 <= pct <= 1``) of an already-sorted sequence."""
    idx = min(int(pct * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[idx]


def _bootstrap_ci(
    signal_returns: Sequence[float],
    baseline_returns: Sequence[float],
    signal_neff: float,
    baseline_neff: float,
    iterations: int,
    seed: int,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """95% bootstrap CIs for (mean signal return, edge over baseline).

    Each iteration resamples ``round(signal_neff)`` / ``round(baseline_neff)``
    draws (with replacement) -- the autocorrelation-adjusted effective sample
    sizes from :func:`_effective_sample_size` -- so CI width reflects the
    independent information in each series rather than the inflated raw
    count of overlapping windows. Deterministic for a given ``seed``.
    """
    rng = random.Random(seed)
    sig_n = max(1, round(signal_neff))
    base_n = max(1, round(baseline_neff))

    means: list[float] = []
    edges: list[float] = []
    for _ in range(iterations):
        sig_mean = sum(rng.choices(signal_returns, k=sig_n)) / sig_n
        base_mean = sum(rng.choices(baseline_returns, k=base_n)) / base_n
        means.append(sig_mean)
        edges.append(sig_mean - base_mean)

    means.sort()
    edges.sort()
    return (
        (_percentile(means, 0.025), _percentile(means, 0.975)),
        (_percentile(edges, 0.025), _percentile(edges, 0.975)),
    )


async def _replay_symbol(
    settings: Settings,
    symbol_repo: SymbolRepository,
    candle_repo: CandleRepository,
    base: str,
    interval: str,
    start: datetime | None,
    end: datetime | None,
    horizons: Sequence[int],
    execution_lag: int,
) -> tuple[list[dict[str, Any]], list[Candle]]:
    """Replay one symbol's candle history through a fresh engine; return its signals + candles."""
    symbol_id = await symbol_repo.get_id(base)
    if symbol_id is None:
        return [], []

    rows = await candle_repo.select_range(symbol_id, interval, start, end)
    candles = [_row_to_candle(base, row) for row in rows]
    interval_ms = _interval_to_ms(interval)

    signal_sink = SignalCaptureSink()
    # NullSink/SignalCaptureSink are duck-typed stand-ins for the
    # BatchWriter/repository types AnalyticsEngine declares -- they implement
    # only the methods it actually calls (.add / .insert / .insert_snapshot).
    engine = AnalyticsEngine(
        settings,
        symbol_repo,
        NullSink(),  # type: ignore[arg-type]
        signal_sink,  # type: ignore[arg-type]
        NullSink(),  # type: ignore[arg-type]
        NullSink(),  # type: ignore[arg-type]
        symbol_ids={base: symbol_id},
    )

    captured: list[dict[str, Any]] = []
    for idx, candle in enumerate(candles):
        before = len(signal_sink.rows)
        await engine.handle_candle(candle)
        for signal_row in signal_sink.rows[before:]:
            captured.append(
                {
                    "ts": datetime.fromtimestamp(candle.ts / 1000, tz=UTC),
                    "type": signal_row.type,
                    "confidence": signal_row.confidence,
                    "close": candle.close,
                    "fwd_returns": _forward_returns(candles, idx, horizons, execution_lag, interval_ms),
                }
            )

    return captured, candles


async def run_signal_validation(
    pool: Any,
    settings: Settings,
    symbols: Sequence[str],
    interval: str = "1m",
    start: datetime | None = None,
    end: datetime | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    execution_lag: int = DEFAULT_EXECUTION_LAG,
    mode: str = PIPELINE_CORRECTNESS,
) -> dict[str, Any]:
    """Replay each symbol independently and return captured signals + summary metrics.

    ``symbols`` are the canonical display symbols (``symbols.base``, e.g.
    ``"BTC"``). Each symbol gets its own fresh ``AnalyticsEngine`` instance
    and its own candle history -- there is no shared state and no merged
    timeline across symbols.

    ``mode`` is ``"pipeline_correctness"`` (default) or ``"edge_evaluation"``.
    The dataset's provenance (REAL vs SYNTHETIC, via
    :func:`~orvixa.backtest.dataset.classify_dataset`) is printed before
    replay. Running ``mode="edge_evaluation"`` against a SYNTHETIC dataset
    raises :class:`ValueError` -- synthetic candles are not market-valid for
    trading evaluation, only for exercising the pipeline.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode!r} (expected one of {VALID_MODES})")

    symbol_repo = SymbolRepository(pool)
    candle_repo = CandleRepository(pool)

    dataset_type = await classify_dataset(symbol_repo, symbols)
    print(f"[signal_validation] dataset: {dataset_type} | mode: {mode}")
    if dataset_type == SYNTHETIC:
        if mode == EDGE_EVALUATION:
            raise ValueError(
                "Refusing to run in 'edge_evaluation' mode on a SYNTHETIC dataset "
                "(symbols tagged 'synthetic_data'). Synthetic candles are not "
                "market-valid for trading evaluation -- only 'pipeline_correctness' "
                "mode is allowed."
            )
        print(
            "[signal_validation] WARNING: dataset is SYNTHETIC. Results below "
            "validate pipeline correctness only and do NOT represent real signal edge."
        )

    signals: dict[str, list[dict[str, Any]]] = {}
    candles_by_symbol: dict[str, list[Candle]] = {}

    for base in symbols:
        captured, candles = await _replay_symbol(
            settings, symbol_repo, candle_repo, base, interval, start, end, horizons, execution_lag
        )
        signals[base] = captured
        candles_by_symbol[base] = candles

    interval_ms = _interval_to_ms(interval)
    metrics = compute_signal_metrics(signals, candles_by_symbol, horizons, execution_lag, interval_ms)
    return {
        "signals": signals,
        "metrics": metrics,
        "dataset_type": dataset_type,
        "mode": mode,
    }


def compute_signal_metrics(
    signals: dict[str, list[dict[str, Any]]],
    candles_by_symbol: dict[str, list[Candle]],
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    execution_lag: int = DEFAULT_EXECUTION_LAG,
    interval_ms: int = DEFAULT_INTERVAL_MS,
    bootstrap_iterations: int = 1000,
    bootstrap_seed: int = 0,
) -> dict[str, Any]:
    """Hit rate, baseline return, edge, and reliability stats -- per signal type/horizon.

    ``edge[type][h] = mean_return[type][h] - baseline_return[h]`` is computed
    in exactly one place, here -- unchanged from before.

    Forward returns at horizon ``h`` come from overlapping ``h``-period
    windows, which inflates the raw sample counts (``sample_size``) and
    overstates how much independent information they carry. Two additional,
    purely descriptive statistics address this without changing the edge or
    baseline formulas above:

    - ``neff`` / ``baseline_neff``: an autocorrelation-adjusted effective
      sample size per type/horizon (see :func:`_effective_sample_size`).
    - ``mean_return_ci_low/high`` and ``edge_ci_low/high``: 95% bootstrap
      confidence intervals (see :func:`_bootstrap_ci`), resampled at the
      effective (not raw) sample size. Deterministic for a given
      ``bootstrap_seed``.
    """
    baseline_pool: dict[int, list[float]] = {h: [] for h in horizons}
    for candles in candles_by_symbol.values():
        per_symbol = _baseline_returns(candles, horizons, execution_lag, interval_ms)
        for h in horizons:
            baseline_pool[h].extend(per_symbol[h])

    baseline_return: dict[int, float | None] = {
        h: (sum(values) / len(values) if values else None) for h, values in baseline_pool.items()
    }
    baseline_neff: dict[int, float | None] = {
        h: _effective_sample_size(values, max_lag=h - 1) for h, values in baseline_pool.items()
    }

    by_type: dict[str, list[dict[str, Any]]] = {}
    for symbol_signals in signals.values():
        for entry in symbol_signals:
            by_type.setdefault(entry["type"], []).append(entry)

    sample_size: dict[str, int] = {t: len(entries) for t, entries in by_type.items()}
    mean_return: dict[str, dict[int, float | None]] = {}
    mean_return_ci_low: dict[str, dict[int, float | None]] = {}
    mean_return_ci_high: dict[str, dict[int, float | None]] = {}
    edge: dict[str, dict[int, float | None]] = {}
    edge_ci_low: dict[str, dict[int, float | None]] = {}
    edge_ci_high: dict[str, dict[int, float | None]] = {}
    neff: dict[str, dict[int, float | None]] = {}
    hit_rate: dict[str, dict[int, float | None]] = {}

    for sig_type, entries in by_type.items():
        mean_return[sig_type] = {}
        mean_return_ci_low[sig_type] = {}
        mean_return_ci_high[sig_type] = {}
        edge[sig_type] = {}
        edge_ci_low[sig_type] = {}
        edge_ci_high[sig_type] = {}
        neff[sig_type] = {}
        for h in horizons:
            returns = [
                e["fwd_returns"][h] for e in entries if e["fwd_returns"][h] is not None
            ]
            mean = sum(returns) / len(returns) if returns else None
            mean_return[sig_type][h] = mean
            base = baseline_return[h]
            edge[sig_type][h] = (mean - base) if (mean is not None and base is not None) else None

            sig_neff = _effective_sample_size(returns, max_lag=h - 1)
            neff[sig_type][h] = sig_neff

            base_neff = baseline_neff[h]
            if returns and baseline_pool[h] and sig_neff is not None and base_neff is not None:
                seed = bootstrap_seed + 1000 * h + sum(ord(c) for c in sig_type)
                (mean_lo, mean_hi), (e_lo, e_hi) = _bootstrap_ci(
                    returns, baseline_pool[h], sig_neff, base_neff, bootstrap_iterations, seed
                )
                mean_return_ci_low[sig_type][h] = mean_lo
                mean_return_ci_high[sig_type][h] = mean_hi
                edge_ci_low[sig_type][h] = e_lo
                edge_ci_high[sig_type][h] = e_hi
            else:
                mean_return_ci_low[sig_type][h] = None
                mean_return_ci_high[sig_type][h] = None
                edge_ci_low[sig_type][h] = None
                edge_ci_high[sig_type][h] = None

        if sig_type in ("buy", "sell"):
            hit_rate[sig_type] = {}
            for h in horizons:
                returns = [
                    e["fwd_returns"][h] for e in entries if e["fwd_returns"][h] is not None
                ]
                if not returns:
                    hit_rate[sig_type][h] = None
                elif sig_type == "buy":
                    hit_rate[sig_type][h] = sum(1 for r in returns if r > 0) / len(returns)
                else:
                    hit_rate[sig_type][h] = sum(1 for r in returns if r < 0) / len(returns)

    return {
        "sample_size": sample_size,
        "baseline_return": baseline_return,
        "baseline_neff": baseline_neff,
        "mean_return": mean_return,
        "mean_return_ci_low": mean_return_ci_low,
        "mean_return_ci_high": mean_return_ci_high,
        "edge": edge,
        "edge_ci_low": edge_ci_low,
        "edge_ci_high": edge_ci_high,
        "neff": neff,
        "hit_rate": hit_rate,
    }
