"""Regime-conditioned overlay on top of :mod:`orvixa.backtest.signal_validation`.

This module is a *second pass*: it treats the output of
:func:`~orvixa.backtest.signal_validation.run_signal_validation` as an
immutable input and adds a ``regime_metrics`` section alongside it. Nothing
here changes ``signals``, ``metrics``, ``dataset_type``, or ``mode`` -- the
existing global ``edge``/``baseline_return``/``hit_rate`` semantics are
untouched.

Per symbol, candle history is replayed a second time -- independently of
:class:`~orvixa.analytics.engine.AnalyticsEngine` and with a fresh
:class:`~orvixa.analytics.indicators.SymbolIndicators` -- to tag every candle
with a *regime* (trend direction x volatility level). Each captured signal is
then bucketed by the regime in effect at its own timestamp, and a
*regime-conditioned baseline* (forward returns pooled only from candles in
the same regime) replaces the global baseline for that bucket's edge.

A signal is also tagged ``isolated``/``clustered`` depending on whether
another signal of the same type fired within ``settings.breakout_window``
candles beforehand -- a diagnostic for the signal-conditioned-selection
caveat noted in the methodological freeze.

This is a decomposition/diagnostic layer, not an inference or alpha
validation system: the ``classification`` labels below are triage hints
("worth a closer, out-of-sample look"), not statistical verdicts -- the same
epistemic boundary documented for :mod:`signal_validation` applies here, just
sliced by regime instead of globally.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..analytics.indicators import SymbolIndicators
from ..analytics.trend import compute_trend
from ..config import Settings
from ..db.repository import CandleRepository, SymbolRepository
from ..feeds.base import Candle
from .signal_validation import (
    DEFAULT_EXECUTION_LAG,
    DEFAULT_HORIZONS,
    PIPELINE_CORRECTNESS,
    _bootstrap_ci,
    _effective_sample_size,
    _forward_returns,
    _interval_to_ms,
    _row_to_candle,
    run_signal_validation,
)

RegimeBucket = tuple[str, str]  # (trend_regime, vol_regime)

# Minimum effective sample size below which a bucket is treated as having no
# usable signal at all.
_MIN_NEFF = 5.0

# A bucket is "diagnostic only" (rather than a candidate worth a closer look)
# if more than this fraction of its signals are clustered with a prior
# same-type signal.
_CLUSTER_DOMINANCE_THRESHOLD = 0.5


def _classify_trend_regime(trend_direction: str | None) -> str:
    """``"up"`` / ``"down"`` / ``"flat"``, or ``"unknown"`` while EMAs warm up."""
    return trend_direction if trend_direction is not None else "unknown"


def _classify_vol_regime(vol_realized: float | None, settings: Settings) -> str:
    """``"low"`` / ``"normal"`` / ``"high"``, or ``"unknown"`` while vol warms up.

    Mirrors :meth:`orvixa.analytics.regime.RegimeEngine._vol_regime`'s
    thresholds (``high_volatility_pct`` / 3) but is reimplemented standalone
    so this module has no dependency on the live, breadth-based
    ``RegimeEngine``.
    """
    if vol_realized is None:
        return "unknown"
    threshold = settings.high_volatility_pct
    if vol_realized >= threshold:
        return "high"
    if vol_realized <= threshold / 3.0:
        return "low"
    return "normal"


def _compute_regime_tags(candles: Sequence[Candle], settings: Settings) -> list[RegimeBucket]:
    """One ``(trend_regime, vol_regime)`` tag per candle, in order.

    Uses a fresh, local :class:`SymbolIndicators` -- no shared state with
    :class:`~orvixa.analytics.engine.AnalyticsEngine` or any other replay.
    The tag for candle ``i`` reflects the indicator/trend state *after*
    folding candle ``i`` in, matching the state a signal fired on that candle
    would have observed.
    """
    indicators = SymbolIndicators(settings)
    tags: list[RegimeBucket] = []
    for candle in candles:
        snapshot = indicators.update(candle)
        trend = compute_trend(snapshot)
        trend_direction = trend.direction if trend is not None else None
        tags.append(
            (
                _classify_trend_regime(trend_direction),
                _classify_vol_regime(snapshot.vol_realized, settings),
            )
        )
    return tags


def _compute_cluster_tags(
    signals: Sequence[dict[str, Any]], window_candles: int, interval_ms: int
) -> list[str]:
    """``"isolated"`` / ``"clustered"`` per signal (assumes ``signals`` sorted by ``ts``).

    A signal is ``"clustered"`` if another signal of the *same type* fired
    strictly within ``window_candles * interval_ms`` milliseconds before it.
    """
    window_ms = window_candles * interval_ms
    tags: list[str] = []
    for i, entry in enumerate(signals):
        ts_ms = int(entry["ts"].timestamp() * 1000)
        clustered = False
        for prior in signals[:i]:
            if prior["type"] != entry["type"]:
                continue
            prior_ts_ms = int(prior["ts"].timestamp() * 1000)
            if 0 < ts_ms - prior_ts_ms <= window_ms:
                clustered = True
                break
        tags.append("clustered" if clustered else "isolated")
    return tags


def _regime_conditioned_baselines(
    candles: Sequence[Candle],
    regime_tags: Sequence[RegimeBucket],
    horizons: Sequence[int],
    execution_lag: int,
    interval_ms: int,
) -> dict[RegimeBucket, dict[int, list[float]]]:
    """Pool :func:`_forward_returns` per candle into its own regime bucket.

    The same "do nothing" reference as
    :func:`~orvixa.backtest.signal_validation._baseline_returns`, but split
    by the regime in effect at each candle instead of pooled globally.
    """
    pools: dict[RegimeBucket, dict[int, list[float]]] = {}
    for idx, bucket in enumerate(regime_tags):
        fwd = _forward_returns(candles, idx, horizons, execution_lag, interval_ms)
        bucket_pool = pools.setdefault(bucket, {h: [] for h in horizons})
        for h, value in fwd.items():
            if value is not None:
                bucket_pool[h].append(value)
    return pools


def _classify_validity(
    neff: float | None,
    edge_ci_low: float | None,
    edge_ci_high: float | None,
    clustered_fraction: float,
) -> str:
    """Triage label for one ``(bucket, signal_type, horizon)`` cell.

    - ``"insufficient_data"``: too few effectively-independent samples (or
      no CI could be computed at all).
    - ``"diagnostic_only"``: the edge CI spans zero, or most of the samples
      are clustered with a prior same-type signal (selection-effect risk).
    - ``"regime_conditional_candidate"``: edge CI excludes zero, samples are
      mostly isolated -- worth a closer, out-of-sample look. Not a
      statistical confirmation of edge.
    """
    if neff is None or neff < _MIN_NEFF:
        return "insufficient_data"
    if edge_ci_low is None or edge_ci_high is None:
        return "insufficient_data"
    if edge_ci_low <= 0.0 <= edge_ci_high:
        return "diagnostic_only"
    if clustered_fraction > _CLUSTER_DOMINANCE_THRESHOLD:
        return "diagnostic_only"
    return "regime_conditional_candidate"


def _bucket_key(trend_regime: str, vol_regime: str) -> str:
    return f"trend={trend_regime},vol={vol_regime}"


def compute_regime_metrics(
    signals: list[dict[str, Any]],
    candles: Sequence[Candle],
    settings: Settings,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    execution_lag: int = DEFAULT_EXECUTION_LAG,
    interval_ms: int = 60_000,
    bootstrap_iterations: int = 1000,
    bootstrap_seed: int = 0,
) -> dict[str, dict[str, Any]]:
    """Regime-bucketed edge/neff/CI for one symbol's captured ``signals``.

    ``signals`` is the per-symbol list from
    ``run_signal_validation(...)["signals"][symbol]`` -- read-only, never
    mutated. Returns ``{bucket_key: {signal_type: {...}}}`` where
    ``bucket_key`` is ``"trend=<up|down|flat|unknown>,vol=<low|normal|high|unknown>"``.

    For each ``(bucket, signal_type, horizon)``:

    - ``edge`` is ``mean(signal returns in this bucket) -
      mean(baseline returns from candles in this same bucket)`` -- a
      regime-conditioned analogue of
      :func:`~orvixa.backtest.signal_validation.compute_signal_metrics`'s
      global ``edge``, computed independently and not used to alter it.
    - ``neff`` / ``edge_ci_low`` / ``edge_ci_high`` reuse the same
      autocorrelation/bootstrap helpers as the global metrics, seeded
      deterministically per ``(horizon, signal_type, bucket)``.
    - ``classification`` is a triage label (see :func:`_classify_validity`).
    """
    if not candles:
        return {}

    regime_tags = _compute_regime_tags(candles, settings)
    cluster_tags = _compute_cluster_tags(signals, settings.breakout_window, interval_ms)
    baseline_pools = _regime_conditioned_baselines(candles, regime_tags, horizons, execution_lag, interval_ms)

    ts_to_idx = {candle.ts: idx for idx, candle in enumerate(candles)}

    by_bucket: dict[RegimeBucket, dict[str, list[tuple[dict[str, Any], str]]]] = {}
    for entry, cluster in zip(signals, cluster_tags, strict=True):
        idx = ts_to_idx.get(int(entry["ts"].timestamp() * 1000))
        if idx is None:
            continue
        bucket = regime_tags[idx]
        by_bucket.setdefault(bucket, {}).setdefault(entry["type"], []).append((entry, cluster))

    out: dict[str, dict[str, Any]] = {}
    for bucket, by_type in by_bucket.items():
        bucket_key = _bucket_key(*bucket)
        baseline_pool = baseline_pools.get(bucket, {h: [] for h in horizons})
        baseline_return = {
            h: (sum(values) / len(values) if values else None) for h, values in baseline_pool.items()
        }
        baseline_neff = {h: _effective_sample_size(values, max_lag=h - 1) for h, values in baseline_pool.items()}

        out[bucket_key] = {}
        for sig_type, tagged_entries in by_type.items():
            entries = [e for e, _ in tagged_entries]
            clustered_fraction = sum(1 for _, c in tagged_entries if c == "clustered") / len(tagged_entries)

            edge: dict[int, float | None] = {}
            edge_ci_low: dict[int, float | None] = {}
            edge_ci_high: dict[int, float | None] = {}
            neff: dict[int, float | None] = {}
            classification: dict[int, str] = {}

            for h in horizons:
                returns = [e["fwd_returns"][h] for e in entries if e["fwd_returns"][h] is not None]
                mean = sum(returns) / len(returns) if returns else None
                base_mean = baseline_return[h]
                edge[h] = (mean - base_mean) if (mean is not None and base_mean is not None) else None

                sig_neff = _effective_sample_size(returns, max_lag=h - 1)
                neff[h] = sig_neff
                base_neff = baseline_neff[h]

                e_lo: float | None
                e_hi: float | None
                if returns and baseline_pool[h] and sig_neff is not None and base_neff is not None:
                    seed = (
                        bootstrap_seed
                        + 1000 * h
                        + sum(ord(c) for c in sig_type)
                        + sum(ord(c) for c in bucket_key)
                    )
                    _mean_ci, (e_lo, e_hi) = _bootstrap_ci(
                        returns, baseline_pool[h], sig_neff, base_neff, bootstrap_iterations, seed
                    )
                else:
                    e_lo = None
                    e_hi = None

                edge_ci_low[h] = e_lo
                edge_ci_high[h] = e_hi
                classification[h] = _classify_validity(sig_neff, e_lo, e_hi, clustered_fraction)

            out[bucket_key][sig_type] = {
                "sample_size": len(entries),
                "clustered_fraction": round(clustered_fraction, 4),
                "neff": neff,
                "edge": edge,
                "edge_ci_low": edge_ci_low,
                "edge_ci_high": edge_ci_high,
                "classification": classification,
            }

    return out


async def run_regime_validation(
    pool: Any,
    settings: Settings,
    symbols: Sequence[str],
    interval: str = "1m",
    start: datetime | None = None,
    end: datetime | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    execution_lag: int = DEFAULT_EXECUTION_LAG,
    mode: str = PIPELINE_CORRECTNESS,
    bootstrap_iterations: int = 1000,
    bootstrap_seed: int = 0,
) -> dict[str, Any]:
    """Run :func:`~orvixa.backtest.signal_validation.run_signal_validation`, then add ``regime_metrics``.

    The returned dict contains everything ``run_signal_validation`` returns
    (``signals``, ``metrics``, ``dataset_type``, ``mode``), unchanged, plus a
    new top-level ``regime_metrics`` key:
    ``{symbol: {bucket_key: {signal_type: {...}}}}`` -- see
    :func:`compute_regime_metrics`.

    Candle history is fetched a second time (independently of the replay
    inside ``run_signal_validation``) purely to compute regime tags; no
    shared state, no new persistence, no new data source.
    """
    result = await run_signal_validation(pool, settings, symbols, interval, start, end, horizons, execution_lag, mode)

    symbol_repo = SymbolRepository(pool)
    candle_repo = CandleRepository(pool)
    interval_ms = _interval_to_ms(interval)

    regime_metrics: dict[str, dict[str, Any]] = {}
    for base in symbols:
        symbol_id = await symbol_repo.get_id(base)
        if symbol_id is None:
            regime_metrics[base] = {}
            continue

        rows = await candle_repo.select_range(symbol_id, interval, start, end)
        candles = [_row_to_candle(base, row) for row in rows]
        regime_metrics[base] = compute_regime_metrics(
            result["signals"].get(base, []),
            candles,
            settings,
            horizons,
            execution_lag,
            interval_ms,
            bootstrap_iterations,
            bootstrap_seed,
        )

    return {**result, "regime_metrics": regime_metrics}
