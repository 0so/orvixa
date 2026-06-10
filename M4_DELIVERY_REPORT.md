# M4 Delivery Report — Deterministic Analytics Engine

Status: **complete**, scoped strictly to the approved M4 plan
("deterministic analytics engine — indicators, trend, signals, events,
regime, health"). No AI/LLM/embeddings/agents/external intelligence
services anywhere in this package — every output is a pure function of
rolling per-symbol state and configured thresholds. M1-M3 architecture is
unmodified. No schema migration was needed (the M2 schema already had
`indicators`/`signals`/`market_events`/`market_memory`); the only repository
change beyond the new batch method is a jsonb-encoding fix that was
"strictly necessary" because M4 is the first milestone to actually write to
those jsonb columns. Milestone 5 has not been started.

## 1. Architecture summary

```
                ┌────────────────────────────┐
                │   MarketFeed (M1, unchanged) │
                │  on_candle_close / on_market_snapshot │
                └───────────┬─────────────┬──────────────┘
                            │             │
                  Candle    │             │ TickerRow[]
                            ▼             ▼
┌──────────────────────────────────────────────────────────────────┐
│                        AnalyticsEngine (M4)                        │
│                                                                      │
│  handle_candle(candle):                                             │
│    1. SymbolIndicators.update -> IndicatorSnapshot                  │
│       (EMA9/EMA21, Wilder RSI/ATR, realized vol, relative volume)   │
│    2. compute_trend(snapshot) -> TrendResult                        │
│       (direction/strength/slope/score)                              │
│    3. queue IndicatorRow -> BatchWriter -> upsert_batch (batched)   │
│    4. SignalEngine.evaluate -> SignalRepository.insert (on          │
│       state transition only: BUY/SELL PRESSURE, HIGH VOLATILITY)    │
│    5. EventEngine.evaluate -> MarketEventRepository.insert          │
│       (breakout/breakdown/pump/dump/vol_spike)                      │
│                                                                      │
│  handle_snapshot(rows) -> BreadthEngine.update (reused from M3)     │
│                                                                      │
│  refresh_regime() [every regime_refresh_interval_seconds]:          │
│    RegimeEngine.evaluate(breadth, trend participation, avg vol)     │
│      -> risk_on/risk_off/rotational + vol_regime + health_score     │
│      -> MarketMemoryRepository.insert_snapshot                      │
└──────────────────────────────────────────────────────────────────┘
```

All per-symbol state (`SymbolIndicators`, rolling deques in `EventEngine`,
state machines in `SignalEngine`, latest `TrendResult`) lives in memory and
is updated incrementally — one candle in, O(1)/O(window) work out. No
full-history recalculation.

### Indicators (`analytics/indicators.py`)

- `EMA` — recursive EMA, seeded with the first observed price, `α = 2/(n+1)`.
  Used for EMA 9 (`ema_fast_period`) and EMA 21 (`ema_slow_period`).
- `WilderRSI` — classic Wilder smoothing: the first value needs `period`
  price changes (seed avg gain/loss = simple mean), then
  `avg = (avg*(n-1) + x) / n`. Default period 14.
- `WilderATR` — same Wilder smoothing applied to True Range. Default
  period 14.
- `RealizedVolatility` — rolling stdev of log returns over
  `realized_vol_window` (default 20), expressed as a percent (`*100`).
- `RelativeVolume` — current candle's volume vs. the average of the prior
  `relative_volume_window` (default 20) candles' volumes.
- `SymbolIndicators` bundles one of each per symbol; `.update(candle)`
  returns an `IndicatorSnapshot` (also carries `ema_slow_prev` for the trend
  slope).

### Trend Engine (`analytics/trend.py`)

`compute_trend(snapshot)` is a pure function of the EMA pair:

- **direction**: `"up"`/`"down"`/`"flat"` based on the sign of
  `(ema_fast - ema_slow) / ema_slow`, with a ±0.05% dead zone for "flat".
- **strength**: `0-100`, the EMA separation (%) scaled by 20 and capped
  at 100 (a 5% separation saturates).
- **slope**: % change of `ema_slow` vs. the previous candle.
- **score**: signed strength (`+strength` up, `-strength` down, `0` flat) —
  persisted as `indicators.trend_score`.

Returns `None` until both EMAs are warmed up (first candle).

### Signal Engine (`analytics/signals.py`)

`SignalEngine` keeps two tiny per-symbol state machines so signals are
emitted **only on transition** (matching `signals.state_from`/`state_to`):

- **pressure** (`"neutral"|"buy"|"sell"`): `"buy"` when trend is up and
  RSI < 70 (not overbought); `"sell"` when trend is down and RSI > 30 (not
  oversold); else `"neutral"`. Confidence (0-100, int) is
  `0.6*trend.strength + 0.2*|RSI-50|*2 + 0.2*clamp((vol_rel-1)*50, 0, 100)`.
  `components` records `trend_direction`, `trend_strength`, `trend_score`,
  `rsi`, `vol_rel`.
- **volatility** (`"normal"|"high"`): `"high"` when `vol_realized >=
  high_volatility_pct`. Confidence is `min(vol_realized/threshold*50, 100)`.
  `components` records `vol_realized` and `threshold`.

A signal is only persisted if the new state differs from the old one *and*
`confidence >= signal_min_confidence` (default 60).

### Event Engine (`analytics/events.py`)

`EventEngine` keeps bounded `deque`s per symbol (highs/lows for
`breakout_window`, closes for `pump_dump_window`, realized-vol history for
`vol_spike_window`) — checks run against history that **excludes** the
current candle, then history is updated:

- **breakout/breakdown**: current close above the prior `breakout_window`
  high, or below the prior low. `magnitude` = % beyond the level.
- **pump/dump**: % change vs. the close `pump_dump_window` candles ago,
  triggered at `±pump_dump_pct` (default 5%).
- **vol_spike**: `vol_realized >= avg(prior vol_spike_window realized-vol
  readings) * vol_spike_multiplier` (default 2x).

`severity` (1-3) is a bucketed magnitude/threshold ratio (≥1.5x -> 2,
≥3x -> 3, else 1). All persisted via `MarketEventRepository.insert`.

### Market Regime Engine (`analytics/regime.py`)

`RegimeEngine.evaluate(breadth, trend_up_frac, trend_down_frac,
avg_vol_realized)`:

- **risk_on**: `ad_ratio >= 1.2`, `pct_above_trend >= 55%`, and more symbols
  trending up than down.
- **risk_off**: `ad_ratio <= 0.8`, `pct_above_trend <= 45%`, and more
  symbols trending down than up.
- **rotational**: everything else.
- **vol_regime**: `"high"` if `avg_vol_realized >= high_volatility_pct`,
  `"low"` if `<= threshold/3`, else `"normal"`.

`breadth` (the M3 `BreadthEngine`, fed by `handle_snapshot`'s whole-market
ticker stream) is reused as-is — no changes to `symbols/breadth.py`.

### Market Health Engine (`analytics/health.py`)

`compute_health_score(breadth, trend_up_frac, vol_regime)` -> `0-100`:

```
score = 0.4 * clamp(ad_ratio / 2, 0, 1) * 100
      + 0.3 * clamp(pct_above_trend, 0, 100)
      + 0.3 * clamp(trend_up_frac, 0, 1) * 100
score += -15 if vol_regime == "high" else (+5 if vol_regime == "low" else 0)
score = clamp(score, 0, 100)
```

### Persistence

- `IndicatorRepository.upsert_batch` (new) — one `executemany` per flush of
  the `BatchWriter[IndicatorRow]`, idempotent on `(symbol_id, ts)` (same
  pattern as `CandleRepository.insert_batch`).
- `SignalRepository.insert`, `MarketEventRepository.insert`,
  `MarketMemoryRepository.insert_snapshot` — fixed to `json.dumps(...)` +
  `::jsonb` cast for `components`/`payload`/`snapshot` (the same fix already
  applied to `SymbolRepository.update_ranking` in M3; these three were never
  exercised before M4 so the bug was latent). Verified live against Postgres.

### Configuration (new `Settings` fields, `# --- analytics engine (M4) ---`)

| Field | Default | Purpose |
|---|---|---|
| `ema_fast_period` / `ema_slow_period` | 9 / 21 | Trend EMAs |
| `rsi_period` / `atr_period` | 14 / 14 | Wilder RSI / ATR |
| `realized_vol_window` | 20 | Realized volatility window |
| `relative_volume_window` | 20 | Relative volume baseline window |
| `breakout_window` | 20 | Breakout/breakdown rolling high/low window |
| `pump_dump_window` / `pump_dump_pct` | 5 / 5.0 | Pump/dump detection |
| `vol_spike_window` / `vol_spike_multiplier` | 20 / 2.0 | Vol-spike baseline + threshold |
| `high_volatility_pct` | 3.0 | HIGH VOLATILITY signal + vol_regime threshold |
| `signal_min_confidence` | 60 | Minimum confidence to persist a signal |
| `regime_refresh_interval_seconds` | 60.0 | Regime/health recompute period |
| `indicator_batch_max_size` / `_interval_seconds` | 200 / 2.0 | Indicator `BatchWriter` tuning |

## 2. File tree (new/changed for M4)

```
src/orvixa/config.py                        (mod) M4 settings block
src/orvixa/db/repository.py                 (mod) IndicatorRepository.upsert_batch (new);
                                                    jsonb fix for SignalRepository.insert,
                                                    MarketEventRepository.insert,
                                                    MarketMemoryRepository.insert_snapshot
src/orvixa/analytics/__init__.py            (new) public exports
src/orvixa/analytics/indicators.py          (new) EMA, WilderRSI, WilderATR,
                                                    RealizedVolatility, RelativeVolume,
                                                    SymbolIndicators, IndicatorSnapshot
src/orvixa/analytics/trend.py               (new) compute_trend, TrendResult
src/orvixa/analytics/signals.py             (new) SignalEngine, SignalResult
src/orvixa/analytics/events.py              (new) EventEngine, EventResult
src/orvixa/analytics/regime.py              (new) RegimeEngine, RegimeResult
src/orvixa/analytics/health.py              (new) compute_health_score
src/orvixa/analytics/engine.py              (new) AnalyticsEngine (core deliverable)
src/orvixa/runners/analytics.py             (new) orvixa-analytics runner
pyproject.toml                              (mod) + orvixa-analytics script entry

tests/test_indicators.py                    (new)
tests/test_trend.py                         (new)
tests/test_signals.py                       (new)
tests/test_events.py                        (new)
tests/test_health.py                        (new)
tests/test_regime.py                        (new)
tests/test_analytics_engine.py              (new)
```

## 3. Performance analysis ("hundreds of symbols efficiently")

- **Per-candle cost is O(1) amortized per symbol**: EMA/RSI/ATR updates are
  O(1); realized volatility, relative volume, breakout/breakdown,
  pump/dump, and vol_spike baselines use bounded `deque`s (`maxlen=window`,
  default ≤20), so each update is O(window) for a `sum()`/`max()`/`min()`
  over at most 20 elements — negligible (<10µs) per symbol per candle.
- **No full-history recalculation**: all state (`SymbolIndicators`,
  `_latest_trend`, `EventEngine`'s deques, `SignalEngine`'s state machines)
  is held in memory and updated incrementally from the previous value plus
  the new candle — never re-read from the database.
- **Indicator persistence is batched**: one `IndicatorRow` per symbol per
  candle close is queued into a `BatchWriter[IndicatorRow]` and flushed via
  a single `executemany` (`IndicatorRepository.upsert_batch`) every
  `indicator_batch_interval_seconds` (default 2s) or
  `indicator_batch_max_size` (default 200) rows — for a universe of e.g. 500
  symbols closing roughly together, that's ~1 round trip per 2s instead of
  500.
- **Signals/events are inherently rare** (only on state transitions /
  threshold crossings), so their per-row `INSERT ... RETURNING id` round
  trips are not a bottleneck even unbatched.
- **Regime/health is O(symbols) once per `regime_refresh_interval_seconds`**
  (default 60s): a single pass over `_latest_trend` + `_indicators` to
  compute fractions/averages, then one `INSERT INTO market_memory`.
- Live smoke test (SimFeed, 11 symbols, ~3s candles, 8s run): 440 candles
  processed, 44 events persisted, 3 regime/health snapshots persisted, no
  errors — see §5.

## 4. Test coverage summary

`PYTHONPATH=src python -m pytest -q` -> **107 passed, 1 skipped** (the skip
is the pre-existing `RUN_DB_TESTS=1`-gated M2 integration test, unrelated to
M4; 38 new M4 tests added to the prior 69).

- **`test_indicators.py`** — EMA seed/smoothing; Wilder RSI/ATR warm-up and
  first-value correctness; realized volatility (stdev of log returns) and
  relative volume math; `SymbolIndicators.update` end-to-end snapshot.
- **`test_trend.py`** — `None` before EMA warm-up; up/down/flat
  classification, signed score, strength saturation, slope sign.
- **`test_signals.py`** — BUY/SELL PRESSURE emitted only on state
  transition (and suppressed on repeat); overbought/oversold suppression;
  low-confidence suppression; HIGH VOLATILITY transition in both directions
  (only the "high" transition persists); missing-input guards.
- **`test_events.py`** — breakout/breakdown require a full window and fire
  correctly; pump/dump over a configurable window/threshold; volatility
  spike vs. rolling baseline; no false positives below thresholds.
- **`test_regime.py`** — risk_on/risk_off/rotational classification across
  breadth + participation combinations; vol_regime low/normal/high; `None`
  avg-vol handled.
- **`test_health.py`** — score formula bounds (0/100 saturation), high-vol
  penalty (-15) and low-vol bonus (+5), full clamping.
- **`test_analytics_engine.py`** — fully fake-driven (`FakePool`,
  `_FakeIndicatorWriter`, no live DB): unclosed/unknown-symbol candles
  dropped; indicator rows queued correctly (warm-up `None`s then populated
  fields incl. `trend_score`); signals and events persisted via the repo
  layer (`INSERT INTO signals` / `INSERT INTO market_events` observed on the
  fake pool); breadth updates from `handle_snapshot`; `refresh_regime`
  no-ops without data and persists `market_memory` once breadth + trend data
  exist.

`ruff check src tests` — clean except the 2 pre-existing M1 `ASYNC110`
findings (unrelated, unchanged since M2/M3).

`mypy src` — `Success: no issues found in 37 source files`.

## 5. Live-DB validation

Ran two checks against the live local Postgres
(`postgresql://orvixa:orvixa@localhost:5432/orvixa`):

1. **Repository round trip**: `IndicatorRepository.upsert_batch`,
   `SignalRepository.insert` (jsonb `components`),
   `MarketEventRepository.insert` (jsonb `payload`), and
   `MarketMemoryRepository.insert_snapshot` (jsonb `snapshot`) all
   insert/round-trip correctly with real `::jsonb` columns.
2. **End-to-end smoke test**: `SimFeed` (11 symbols, `sim_candle_seconds=0.2`)
   wired straight into `AnalyticsEngine` for 8s:
   - 440 closed candles processed, all queued as `IndicatorRow`s and flushed
     via `upsert_batch`.
   - 44 market events persisted (breakouts/breakdowns from the sim feed's
     random-walk prices).
   - 3 `market_memory` snapshots persisted (`risk_off`/`rotational`
     regimes, `vol_regime` low/normal, `health_score` 33-59), each with a
     valid jsonb `snapshot` (`ad_ratio`, `pct_above_trend`,
     `trend_up_frac`/`trend_down_frac`, `avg_vol_realized`).
   - `latest indicator` for BTC showed populated `ema_fast`/`ema_slow`/`rsi`
     /`atr`/`vol_realized`/`vol_rel`/`trend_score` (0, i.e. flat trend on the
     sim's near-random walk — expected).
   - No signals crossed `signal_min_confidence` during this short run with
     default thresholds (sim data is low-volatility/random-walk); the signal
     persistence path itself was separately verified in check 1.

All scratch/manual rows created during validation were deleted afterward;
`symbols`/`indicators`/`signals`/`market_events`/`market_memory` are back to
their pre-validation state.

## 6. Definition-of-done validation

| # | Requirement | Status |
|---|---|---|
| 1 | EMA9/EMA21/RSI/ATR/Realized Vol/Relative Volume, stored in `indicators` | ✅ `analytics/indicators.py`, `IndicatorRepository.upsert_batch` |
| 2 | Trend direction/strength/slope per symbol | ✅ `analytics/trend.py`, persisted as `indicators.trend_score` |
| 3 | BUY/SELL PRESSURE + HIGH VOLATILITY signals with confidence/score/factors, persisted | ✅ `analytics/signals.py`, `SignalRepository.insert` |
| 4 | breakout/breakdown/pump/dump/vol_spike events, persisted | ✅ `analytics/events.py`, `MarketEventRepository.insert` |
| 5 | Risk-On/Risk-Off/Rotational regime from breadth + trend participation | ✅ `analytics/regime.py` (reuses M3 `BreadthEngine`) |
| 6 | 0-100 market health score | ✅ `analytics/health.py` |
| 7 | Hundreds of symbols, no full-history recalculation, incremental | ✅ in-memory per-symbol state, bounded deques, batched indicator writes (§3) |
| 8 | Unit/indicator/signal/event/regime tests | ✅ 38 new tests, 107 passed/1 skipped total |
| 9 | analytics package + indicators/signal/event/regime/health engines + tests | ✅ all present (file tree above) |

**Constraints honored**: no AI/LLM/embeddings/agents/external services; no
feed/architecture changes; no schema migration (only the jsonb-encoding fix
to existing M2 repository methods, justified as strictly necessary since M4
is the first milestone to write those columns); Milestone 5 not started.

M4 is complete and ready for review.
