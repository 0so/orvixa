# M4 Validation Report (Final) — Deterministic Analytics Engine

Independent audit of Milestone 4 prior to starting Milestone 5. Scope:
mathematical correctness of all indicators/engines, edge cases, duplicated
signals/event spam, performance, memory, race conditions, and persistence
consistency. **No M5 work was started.**

## Summary

| Area | Result |
|---|---|
| Indicator math (EMA, Wilder RSI/ATR, realized vol, relative volume) | ✅ Correct, no changes |
| Trend engine | ✅ Correct, no changes |
| Regime engine / health score | ✅ Correct, no changes |
| Signal engine (BUY/SELL PRESSURE, HIGH VOLATILITY) | 🐛 2 bugs found and fixed |
| Event engine (breakout/breakdown/pump/dump/vol_spike) | 🐛 1 major bug found and fixed (event spam) |
| `AnalyticsEngine.refresh_regime` | 🐛 1 race-condition risk found and fixed |
| Test suite | 107 → **112 passed**, 1 skipped (5 new regression tests) |
| `ruff` / `mypy` | Clean (2 pre-existing `ASYNC110` findings in `test_reconnect.py`, unrelated to M4, predate this audit) |
| Performance (300 symbols × 500 candles = 150k candles) | ~169 µs/candle, ~25s total |
| Memory | Bounded — all rolling history uses `deque(maxlen=...)`; ~9.5 MiB peak for 300 symbols |

---

## 1. Indicator calculations (`analytics/indicators.py`)

Reviewed `EMA`, `WilderRSI`, `WilderATR`, `RealizedVolatility`, `RelativeVolume`,
and `SymbolIndicators`.

- **EMA**: standard recursive EMA with `alpha = 2/(period+1)`, seeded with the
  first observed value. Verified against `test_ema_seeds_with_first_value_then_smooths`.
- **Wilder RSI**: correct Wilder smoothing; seed average gain/loss is the
  simple mean of the first `period` price changes, matching the reference
  implementation. Verified against `test_wilder_rsi_warms_up_then_computes`.
- **Wilder ATR**: correct True Range + Wilder smoothing, seeded with the
  simple mean of the first `period` TR values.
- **Realized volatility**: population standard deviation of log returns over
  a rolling window, scaled ×100 — matches `test_realized_volatility_warms_up_then_computes`.
- **Relative volume**: `RelativeVolume.update` computes the ratio against the
  average of the **prior** window (current volume is appended *after* the
  ratio is computed), so it never leaks the current candle's volume into its
  own baseline. Verified correct.

No edge-case issues found. (`period=0` for `WilderRSI`/`WilderATR` would raise
`ZeroDivisionError`, but this is a configuration-time misuse, not reachable
through any validated `Settings` value, and out of scope for this audit.)

## 2. Trend engine (`analytics/trend.py`)

`compute_trend` correctly:
- Returns `None` only when EMAs are not yet initialized (`ema_fast`/`ema_slow`
  is `None`) or `ema_slow == 0` (avoids division by zero).
- On the very first candle (`ema_fast == ema_slow == close`, no `ema_slow_prev`),
  correctly returns a `flat` result with `score == 0`, `slope == 0` — this is
  intentional (EMAs *are* initialized after one candle) and matches
  `test_uptrend_direction_strength_and_slope` / `test_flat_when_separation_below_threshold`.
- Strength saturates at 100 for separations ≥ `_STRENGTH_SCALE` (20%), and the
  flat threshold (`_FLAT_THRESHOLD_PCT = 0.05%`) is applied correctly.

No issues found.

## 3. Signal engine (`analytics/signals.py`) — **2 bugs fixed**

### Bug 1 — `_evaluate_pressure`: low-confidence transitions permanently suppressed future signals

**Before:** the per-symbol pressure state (`"neutral"|"buy"|"sell"`) was
updated to `new_state` *before* the confidence gate was checked:

```python
old_state = self._pressure_state.get(symbol_id, "neutral")
self._pressure_state[symbol_id] = new_state
if new_state == old_state or new_state == "neutral":
    return None
confidence = self._pressure_confidence(trend, snapshot)
if confidence < self._settings.signal_min_confidence:
    return None
```

If a "buy" transition occurred with `confidence < signal_min_confidence`, the
state was already advanced to `"buy"` even though nothing was emitted. On a
later candle where confidence *would* be sufficient, `new_state == old_state
== "buy"` short-circuited the function — the legitimate signal was **lost
permanently** until the trend cycled back through "neutral".

**Fix:** state is now only updated to `"neutral"` immediately (always safe to
record), and only updated to `"buy"`/`"sell"` **after** the confidence check
passes and the signal is actually emitted. A low-confidence transition no
longer "uses up" the state transition.

### Bug 2 — `_evaluate_highvol`: identical pattern for HIGH VOLATILITY

**Before:** `self._highvol_state[symbol_id] = new_state` ("high") was set
before the confidence check
(`confidence = round(min(vol_realized/threshold*50, 100))`). For
`vol_realized` between `threshold` and `1.2×threshold`, confidence falls in
`[50, 100)`, which is below the default `signal_min_confidence=60` —
permanently blocking future "high" emissions until volatility first dropped
back to "normal".

**Fix:** mirrors Bug 1 — state only flips to `"high"` once the signal is
actually emitted; state always resets to `"normal"` immediately when
volatility drops below threshold.

### Regression tests added (`tests/test_signals.py`)

- `test_low_confidence_transition_does_not_block_later_signal` — a
  low-confidence "buy" transition (confidence ≈ 6) is suppressed and the
  state remains `"neutral"`; a subsequent high-confidence "buy" candle
  (confidence ≈ 74) now correctly emits.
- `test_low_confidence_highvol_transition_does_not_block_later_signal` — same
  pattern for HIGH VOLATILITY (confidence ≈ 58 then ≈ 67).

All 6 pre-existing `test_signals.py` tests continue to pass unchanged.

## 4. Event engine (`analytics/events.py`) — **1 major bug fixed (event spam)**

### Bug 3 — no transition/dedup logic at all

**Before:** `_check_breakout`, `_check_pump_dump`, and `_check_vol_spike` each
fired on **every** candle for which their threshold condition held, with no
state tracking. During any sustained breakout, rally/selloff, or volatility
regime spanning N consecutive candles, this produced **N duplicate event
rows** instead of 1 — directly matching the audit's "duplicated
signals"/"event spam" criteria.

**Fix:** added three per-symbol state machines to `EventEngine`
(`_breakout_state: "none"|"up"|"down"`, `_pump_dump_state: "none"|"pump"|"dump"`,
`_vol_spike_state: "none"|"spike"`). Each check now:
- emits **only on transition into** the triggering condition,
- resets to `"none"`/`"normal"` as soon as the condition no longer holds, so a
  *subsequent new* breach can fire again.

### Regression tests added (`tests/test_events.py`)

- `test_breakout_does_not_repeat_until_reset` — a sustained breakout emits
  once, a further extension does **not** re-emit, a pullback resets state, and
  a new breakout above the new rolling high fires again.
- `test_pump_does_not_repeat_for_sustained_pump` — sustained pump conditions
  emit once, not on every candle.
- `test_vol_spike_does_not_repeat_for_sustained_spike` — sustained vol-spike
  conditions emit once, not on every candle.

All 6 pre-existing `test_events.py` tests continue to pass unchanged (each
only exercises a single triggering candle, so the new transition-only
semantics are backward compatible).

### Quantified spam reduction

A 5,000-candle random-walk simulation (default `breakout_window=20`) was
compared against a naive "fire on every candle where the threshold condition
holds" implementation:

- Naive (no dedup): **1,043** breakout/breakdown events
- State-machine (deduped): **578** events
- **44.6% reduction** in emitted breakout/breakdown rows for the same price
  series, with no loss of "first occurrence" signals.

## 5. Regime engine / health score (`analytics/regime.py`, `analytics/health.py`)

Both re-reviewed and confirmed stateless and correct:

- `RegimeEngine.evaluate` correctly classifies `risk_on`/`risk_off`/`rotational`
  from breadth `ad_ratio`/`pct_above_trend` plus trend participation
  fractions, and `vol_regime` (`low`/`normal`/`high`) from `avg_vol_realized`
  vs `high_volatility_pct` (with a sensible "no data → normal" default).
- `compute_health_score` is a clamped 0–100 weighted blend
  (`0.4*ad_component + 0.3*pct_above_trend + 0.3*participation`, ±15/+5
  vol-regime adjustment). All 4 `tests/test_regime.py` cases pass.

No changes made.

## 6. `AnalyticsEngine` orchestrator (`analytics/engine.py`) — **1 race-condition risk fixed**

### Bug 4 — `refresh_regime` iterating `self._indicators.values()` without a defensive copy

`refresh_regime()` runs as a periodic background task
(`_loop` → `asyncio.create_task`), concurrently with `handle_candle()`
(invoked from feed callbacks). `handle_candle` can insert a new key into
`self._indicators` via:

```python
state = self._indicators.setdefault(symbol_id, SymbolIndicators(self._settings))
```

`refresh_regime` already defensively copies `self._latest_trend.values()` via
`list(...)`, but the `vols` comprehension iterated `self._indicators.values()`
**directly**. If a brand-new symbol's first candle arrives mid-iteration, this
can raise `RuntimeError: dictionary changed size during iteration` — the same
bug class previously found and fixed in M3's `SymbolManager.refresh_universe()`.

**Fix:** wrapped in `list(...)`:

```python
vols = [
    state.vol_realized.value
    for state in list(self._indicators.values())
    if state.vol_realized.value is not None
]
```

This is a read-only iteration (unlike M3's `_persist`, which also mutated
`_states` during iteration), so a lightweight defensive-copy is sufficient —
no `asyncio.Lock()` needed.

### `runners/analytics.py` lifecycle review

Reviewed shutdown ordering: `engine.stop()` → `feed.stop()` →
`indicator_writer.stop()` (flushes any remaining batched `IndicatorRow`s) →
`pool.close()`. Order is correct — no orphaned in-flight writes or
use-after-close issues found. No changes made.

## 7. Persistence consistency

- `IndicatorRepository.upsert_batch`, `SignalRepository.insert`,
  `MarketEventRepository.insert`, `MarketMemoryRepository.insert_snapshot` —
  jsonb-encoding fixes from the M4 delivery were re-spot-checked; `components`/
  `payload`/`snapshot` dicts are JSON-encoded before being passed to
  `asyncpg`. No regressions from this audit's changes (none of the fixes
  touch repository/persistence code — all three bug fixes are confined to
  `analytics/signals.py`, `analytics/events.py`, and `analytics/engine.py`'s
  in-memory state).
- `SignalRow`/`MarketEventRow` are now emitted **less frequently** (transition-only),
  which reduces write volume without changing the row schema or insert paths.

## 8. Performance validation

Simulated 300 symbols × 500 candles (150,000 total `handle_candle` calls,
random-walk price series, default M4 settings, `FakePool`/null indicator
writer to isolate engine cost):

```
symbols=300 candles_per_symbol=500 total_candles=150000
elapsed=25.357s  per_candle=169.05us
candles_processed=150000
signals_emitted=0
events_emitted=16487
regime_refresh_count=10
```

- ~169 µs/candle (with `tracemalloc` active, which adds overhead) — at this
  rate, even a 1-second-candle universe of 300 symbols (300 candles/sec)
  processes in ~50 ms of engine time per second of wall-clock, comfortably
  within budget.
- `signals_emitted=0` is expected for a low-volatility random walk: trend
  `strength` (which dominates the pressure-confidence formula at weight 0.6)
  stays near zero when EMA fast/slow barely diverge, so `confidence` rarely
  reaches the default `signal_min_confidence=60`.
- `events_emitted=16487` (~11% of candles) reflects the now-deduped breakout/
  breakdown/pump/dump/vol_spike rate across 300 independent random walks with
  `breakout_window=20` — consistent with the dedup analysis in §4.
- `regime_refresh_count=10` confirms the periodic regime/health refresh runs
  as expected (called manually every 50 candles in the simulation).

## 9. Memory validation

- `tracemalloc` peak for the 150k-candle / 300-symbol run: **~9.5 MiB**.
- All rolling-history buffers in `EventEngine` (`_highs`, `_lows`, `_closes`,
  `_vol_history`) use `deque(maxlen=...)` — confirmed bounded at
  `breakout_window`/`pump_dump_window`/`vol_spike_window` regardless of run
  length (spot-checked: `maxlen=20 len=20`, `maxlen=5 len=5` after 150k
  candles).
- Per-symbol state dicts (`_pressure_state`, `_highvol_state`,
  `_breakout_state`, `_pump_dump_state`, `_vol_spike_state`,
  `self._indicators`, `self._latest_trend`) grow with the **number of distinct
  symbols**, not with candle count — bounded by the universe size, as
  expected.

## 10. Test suite

```
112 passed, 1 skipped
```

(up from 107 passed / 1 skipped before this audit — 5 new regression tests
added: 2 in `tests/test_signals.py`, 3 in `tests/test_events.py`).

`ruff check src tests` and `mypy src` are clean for all M4 code. The 2
`ASYNC110` findings in `tests/test_reconnect.py` predate this audit (confirmed
via `git stash`) and are unrelated to the M4 analytics package — left
untouched per the "no architecture/schema redesign beyond what's strictly
necessary" constraint and because they're outside this audit's scope (M1 feed
test fakes).

## 11. Files changed in this audit

- `src/orvixa/analytics/signals.py` — Bug 1 & 2 fixes (state-machine ordering).
- `src/orvixa/analytics/events.py` — Bug 3 fix (transition-only event emission
  + 3 new per-symbol state dicts).
- `src/orvixa/analytics/engine.py` — Bug 4 fix (defensive `list()` copy in
  `refresh_regime`).
- `tests/test_signals.py` — 2 new regression tests.
- `tests/test_events.py` — 3 new regression tests.

No schema changes, no new dependencies, no AI/LLM/embeddings/agents/external
services introduced. **M5 was not started.**
