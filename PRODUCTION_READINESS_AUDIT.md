# Orvixa — Production Readiness Audit

**Scope:** Full read of `src/orvixa/**` (analytics, api, backfill, backtest, db, feeds, persistence, runners, symbols), `alembic/`, `frontend/`, `tests/`, Docker/Compose files, `pyproject.toml`, `Makefile`, `scripts/`, and the team's own M1-M4 + Phase 1/2 delivery/validation reports.

**Panel:** CTO, Staff Engineer, Principal Architect, DevOps Lead, Security Reviewer.

**Verdict up front:** This is a well-organized, well-documented codebase for what it is — a single-operator analytics/ingest pipeline with a thin read-only API and a static dashboard. It is meaningfully better-engineered than typical early-stage repos: parameterized SQL throughout, an honest internal validation history (bugs found and fixed across milestones, with regression tests added), structured JSON logging, and a real dev/prod Compose split. **It is not, however, ready to be trusted with real users or real money.** Several core analytics claims are unproven or non-functional on real data, the production Docker image is the dev image, the headline TimescaleDB feature has never been run against real TimescaleDB, a money-relevant DB write path will throw in production, the dynamic Symbol Manager (M3) is not deployed at all, and there is zero metrics/alerting infrastructure beyond JSON logs.

---

## 1. Architecture

**Overall:** The milestone narrative (M1 feeds → M2 persistence → M3 symbol manager → M4 analytics → M5 read-only API) is mostly reflected in the code, with one major exception (M3 is not wired into deployment at all — see below). Separation of concerns is generally good: `feeds/` knows nothing about persistence; `persistence/` knows nothing about analytics; `analytics/` is pure-ish and stateful only via small per-symbol dicts; `api/` is read-only and imports only the repositories it needs.

### Findings

**1.1 — [HIGH] M3 Symbol Manager is not deployed — dynamic discovery/tiering never runs**
- Location: `docker-compose.dev.yml`, `docker-compose.prod.yml` (no `orvixa-symbols` / `SymbolManager` service); `src/orvixa/runners/symbols.py` (103 lines, fully implemented); `src/orvixa/symbols/manager.py`.
- Verified via `grep -n "symbols\|orvixa-symbols\|SymbolManager" docker-compose.dev.yml docker-compose.prod.yml` — only services present are `migrate`, `ingest`, `analytics`, `api`, `frontend` (+ `backfill` profile in dev).
- Business impact: M3's entire value proposition — automatic discovery of new listings/delistings, volume-based ranking, Tier 0/1/2 watchlist management, and spike promotion/demotion — never executes in either dev or prod stacks. The system runs forever on the static `seed_symbols()` set from `.env` (`CORE_SYMBOLS`/`SEED_SYMBOLS`), with tier/rank/tags frozen at whatever `persistence/registry.py` wrote at startup.
- Production risk: New high-volume listings (which is exactly the scenario M3 was built to detect — "spiking" Tier-2 symbols) are silently invisible to the platform. Delisted symbols are never marked `frozen`. Any dashboard or alert that claims to reflect "the live tradable universe" is actually showing a static config snapshot.
- Recommended fix: Add an `orvixa-symbols` service to both compose files (mirroring `ingest`/`analytics`'s `<<: *app` pattern), running `python -m orvixa.runners.symbols` under `daemon.py` supervision.
- Effort: S (the runner and manager are fully implemented and tested — this is purely a deployment-config gap).

**1.2 — [HIGH] No exception handling around any of the three periodic "_loop" tasks — silent permanent death**
- Location: `src/orvixa/symbols/manager.py:134-142` (`SymbolManager._loop`), `src/orvixa/analytics/engine.py` `_loop` (around lines 126-134), contrast with `src/orvixa/feeds/binance.py:163-184` (`BinanceFeed._run`, which does catch broad `Exception`).
- Business impact: Any unhandled exception inside `refresh_universe()` (e.g., a transient DB error during `_persist`, or — once M3 is deployed — the uncaught `_sync_feed` exception path described in 1.3) or `refresh_regime()` permanently kills that `asyncio.Task`. There is no restart, and `asyncio` does not surface "Task exception was never retrieved" unless something calls `.result()` or the loop is in debug mode.
- Production risk: The symbol-tiering loop or the regime/health-score loop can die silently on a single transient error and never run again for the lifetime of the process — with `daemon.py`'s crash-restart supervision operating at the *process* level, not the *task* level, this won't trigger a restart because the process itself stays "alive" (the `asyncio.create_task` just never gets awaited again).
- Recommended fix: Wrap the cycle body of each `_loop` in `try/except Exception: logger.exception(...)` (matching `BinanceFeed._run`'s pattern), so a single bad cycle is logged and the loop continues to the next interval.
- Effort: S.

**1.3 — [MEDIUM] `_sync_feed` exception would crash `SymbolManager._loop` entirely**
- Location: `src/orvixa/symbols/manager.py:375-393` (`_sync_feed`), called unguarded from `refresh_universe` (line 217-218) inside `_refresh_lock`.
- Business impact: If `await self._feed.subscribe(new)` succeeds but `await self._feed.unsubscribe(dropped)` raises, `self._subscribed` (line 393) is never updated — combined with finding 1.2, this would kill the whole refresh loop, not just this cycle.
- Recommended fix: combine with 1.2's fix (loop-level try/except is sufficient), but additionally consider try/except around the subscribe/unsubscribe pair so a partial failure doesn't leave `_subscribed` stale relative to `_states`.
- Effort: S.

**1.4 — [MEDIUM] Known, unfixed symbol-collision bug — `normalize_symbol` + `SymbolManager._states` keying**
- Location: `src/orvixa/feeds/normalize.py` (`_THOUSAND_PREFIX`/suffix-stripping logic, lines 25-31), `src/orvixa/symbols/manager.py:251-272` (`_sync_listings`, keys `self._states` by `base`).
- Documented in `M3_VALIDATION_REPORT_FINAL.md` §3.2 as "reported, not fixed", severity "low" by the team.
- Business impact: `PEPEUSDT` and `1000PEPEUSDT` both normalize to base `"PEPE"`. If both are simultaneously active on Binance, the second one processed in `_sync_listings` silently overwrites the first's `_SymbolState` — **with no error, no log**. One pair's market data, tier assignment, and feed subscription silently disappear.
- Production risk: This is exactly the kind of issue that surfaces only when a "1000X"-prefixed meme coin (a recurring Binance pattern, e.g. `1000SHIB`, `1000PEPE`, `1000FLOKI`) and its un-prefixed counterpart are both listed — a realistic, not hypothetical, scenario.
- Recommended fix: Key `_states` by `pair` (the actual Binance symbol) instead of normalized `base`, or detect and log collisions explicitly and pick a deterministic winner (e.g. prefer the un-prefixed pair) with an alert.
- Effort: M (touches `_states` keying throughout `manager.py`, plus DB schema implications since `symbols.base` is presumably unique).

**1.5 — [MEDIUM] `_PROACTIVE_RECONNECT_SECONDS` is dead code — module docstring overstates resilience**
- Location: `src/orvixa/feeds/binance.py:48` (`_PROACTIVE_RECONNECT_SECONDS = 23 * 3600`), docstring lines 8-9 claim a proactive reconnect "well before Binance's 24h server-side cap".
- Business impact: Low by itself (the *reactive* reconnect path still works when Binance force-closes at 24h), but it's a documentation/implementation mismatch that could mislead an on-call engineer debugging a "candle gap around the 24h mark" incident into believing proactive reconnection is in place when it isn't.
- Recommended fix: either implement the proactive reconnect (schedule a clean reconnect at `_PROACTIVE_RECONNECT_SECONDS` after each successful connect) or remove the constant and correct the docstring.
- Effort: S (remove dead code + fix docs) to M (implement proactive reconnect properly).

**1.6 — [LOW] Architectural inconsistency: two different code paths can seed the `symbols` table**
- Location: `src/orvixa/persistence/registry.py:49-51` (`seed_symbols`, called by `runners/ingest.py` and `runners/analytics.py`) vs. `src/orvixa/symbols/manager.py` `_load_existing` + `_persist`'s `upsert` (used by `runners/symbols.py`, which does **not** call `seed_symbols`).
- Production risk: Whichever runner starts first determines the initial classification path for `core`/`meme`/`alt`. The *outcome* is likely equivalent today (both use the same `core_symbols`/`meme_symbols` config), but it's two divergent code paths producing the same table — fragile if either path's classification logic changes independently.
- Recommended fix: Have `runners/symbols.py` also call `seed_symbols` at startup for consistency, or consolidate into one shared seeding function.
- Effort: S.

**1.7 — [LOW] Hardcoded regime-classification thresholds break the config-everything pattern**
- Location: `src/orvixa/analytics/regime.py:20-23` (`_RISK_ON_AD_RATIO=1.2`, `_RISK_ON_PCT_ABOVE_TREND=55.0`, `_RISK_OFF_AD_RATIO=0.8`, `_RISK_OFF_PCT_ABOVE_TREND=45.0`) vs. nearly every other threshold (`high_volatility_pct`, `pump_dump_pct`, `signal_min_confidence`, etc.) being `Settings` fields in `config.py`.
- Production risk: Tuning regime classification (a frequent need during initial calibration against real data — see Section 2/8 on the Phase 1 zero-signal finding) requires a code change + redeploy rather than an env var change.
- Recommended fix: Move these four constants to `Settings`.
- Effort: S.

---

## 2. Reliability

### Findings

**2.1 — [CRITICAL] BatchWriter silently drops up to `max_size` rows on any sink failure — no retry, no dead-letter**
- Location: `src/orvixa/persistence/batch_writer.py:95-105` (`_flush`).
- Code: `batch, self._buffer = self._buffer, []` happens *before* `await self._sink(batch)` is attempted (line ~99-101); if `self._sink(batch)` raises, `error_count` is incremented and the exception is logged via `logger.exception`, but `batch` is gone forever.
- Business impact: A single transient Postgres error (connection pool exhaustion, deadlock, brief network blip) during a flush silently and permanently drops up to `max_size` (default 200, per `.env.example`) candles or indicator rows. For a market-data product, this is **silent data loss in the core pipeline** — exactly the kind of gap that goes unnoticed until someone asks "why is there a 3-minute hole in BTCUSDT's candles from last Tuesday?"
- Production risk: Compounds with finding 2.2 (`refresh_regime` going silent) — both failure modes produce no operator-visible signal beyond a log line that nothing currently scans/alerts on.
- Also affects `stop()` (lines 59-68): the final flush on shutdown can fail and lose the last in-flight batch with no propagation.
- Recommended fix: At minimum, on sink failure, re-prepend the failed batch to `self._buffer` for one retry on the next flush cycle (with a max-retry cap to avoid unbounded buffer growth on a persistently-down DB), and expose `error_count`/dropped-row counts via a metrics endpoint (see Section 10). A proper fix would add a bounded on-disk or in-memory dead-letter queue.
- Effort: M.

**2.2 — [HIGH] `refresh_regime` silently stops persisting `market_memory` during sustained feed outages — zero alerting**
- Location: `src/orvixa/analytics/engine.py` `refresh_regime` (~line 224): `if self._latest_breadth is None or not self._latest_trend: return None`.
- `self._latest_breadth` is only ever set via `handle_snapshot`, which depends on `BinanceFeed` having an active `!miniTicker@arr` subscription. During a sustained reconnect-backoff window (backoff steps up to 30s+jitter, `max_reconnects=None` i.e. infinite retries in production per `binance.py:45`), `_latest_breadth` stays `None`.
- Business impact: `market_memory` (the regime/health-score table backing `/regime/{symbol}`) simply stops being written. The only observable symptom is `regime_refresh_count` failing to increment in logs — nothing alerts on this.
- Recommended fix: Log a WARNING the first time `refresh_regime` no-ops due to missing breadth data (and periodically thereafter, e.g. every N consecutive no-ops), and expose `regime_refresh_count`/`last_regime_refresh_ts` via `/health` or a metrics endpoint.
- Effort: S-M.

**2.3 — [HIGH] `MarketReportRepository.insert` will raise `asyncpg.exceptions.DataError` against a real Postgres pool**
- Location: `src/orvixa/db/repository.py:379-397`.
- `row.scenarios` (a Python `dict`, per `db/models.py` `MarketReportRow.scenarios: dict[str, Any] = field(default_factory=dict)`) is passed directly as a bind parameter to a `jsonb` column with **no `json.dumps()` and no `::jsonb` cast** — unlike every sibling jsonb write in the same file: `SignalRepository.insert` (line 289, `json.dumps(row.components)` + `$5::jsonb`), `MarketEventRepository.insert` (line 327, `json.dumps(row.payload)` + `$7::jsonb`), `MarketMemoryRepository.insert_snapshot` (line 362, `json.dumps(row.snapshot)` + `$6::jsonb`).
- Why it's invisible today: `FakePool` (used by `tests/test_repository.py:172-181`) does not perform asyncpg's actual type encoding/serialization, so the test passes a raw dict straight through with no error. The production write pool (`db/pool.py::create_pool`) also registers **no jsonb codec** (unlike the read-only pool in `api/deps.py`), so even if the dict were accepted by asyncpg's default codec path it would not round-trip correctly.
- Business impact: M6 (`market_reports`) is not yet implemented as a runner, so this is **not currently exercised in production** — but it is a guaranteed first-run failure the moment M6 ships, and it's the kind of bug that "164 passed, 1 skipped" gives false confidence about.
- Recommended fix: `json.dumps(row.scenarios)` + `$3::jsonb` (matching the established pattern), and add a `FakePool`/integration test that actually serializes jsonb (or extend `test_db_integration.py`).
- Effort: S (one-line fix + test).

**2.4 — [HIGH] No application-level liveness/heartbeat watchdog on the WebSocket feed**
- Location: `src/orvixa/feeds/binance.py:190-212` (`_connect_once`), `_default_connector` (lines 266-269): `websockets.connect(url, ping_interval=20, ping_timeout=20)`.
- Business impact: If Binance silently stops sending data on a half-open connection that the underlying `websockets` library's ping/pong doesn't detect promptly, the feed can sit "connected" (per `wait_connected`) but receive nothing — `_run`'s `attempt = 0` reset only happens on a clean return from `_connect_once`, which itself only returns when the `async for raw in ws` iterator ends. There's no "no message received in N seconds → force reconnect" check independent of the library's own keepalive.
- Recommended fix: Track `_last_message_at` and have `_run` (or a separate watchdog task) force-close and reconnect if no message (including pings) has been received within e.g. 60s.
- Effort: M.

**2.5 — [MEDIUM] Gap-fill failure on reconnect is silent and unretried — contradicts "no minute is ever lost" claim**
- Location: `src/orvixa/feeds/binance.py:257-259` (`_gap_fill`, broad `except Exception` + `logger.exception("gap-fill failed")`, `gapfill_count` not incremented on failure).
- Business impact: If the REST backfill call fails entirely right after a reconnect (a plausible scenario — reconnects often correlate with transient network issues that would also affect the REST endpoint), the candles missed during the outage are **never backfilled**, with only a log line as evidence. The module docstring's "no minute is ever lost" claim does not hold under this failure mode.
- Compounding issue: `_default_backfiller` (lines 271-285) issues REST calls **sequentially per symbol** (no `asyncio.gather`), so for a 20-30 symbol watchlist, a reconnect triggers 20-30 sequential round trips before gap-fill completes — and partial failures (some symbols backfilled, others not) are possible with no overall failure signal.
- Recommended fix: Retry gap-fill with backoff (a few attempts) before giving up; parallelize per-symbol backfill calls with `asyncio.gather` (with a concurrency cap to respect Binance rate limits); track and expose a `gapfill_failures` counter.
- Effort: M.

**2.6 — [MEDIUM] Unvalidated numeric config can crash the analytics engine on the first candle**
- Location: `src/orvixa/config.py` (no `Field(gt=0)` on `rsi_period`, `atr_period`, `ema_fast_period`, `ema_slow_period`, `realized_vol_window`, `relative_volume_window`, `breakout_window`, `pump_dump_window`, `vol_spike_window`, etc.); `src/orvixa/analytics/indicators.py` `WilderRSI`/`WilderATR` (`self._avg_gain = sum(self._seed_gains) / self.period` — `ZeroDivisionError` if `period=0`); `src/orvixa/analytics/events.py:192-204` (`deque(maxlen=...)` — `ValueError: maxlen must be non-negative` if a window setting is negative).
- Business impact: A typo'd `.env` (`RSI_PERIOD=0`) is not caught at config-load time despite `config.py` having real `field_validator`s for security-relevant fields (lines 135-164). The crash happens inside `handle_candle`, caught only by `feeds/base.py`'s broad per-callback `except Exception` (line 94) — so the *symptom* is a recurring "feed callback raised" log line on every candle, for every symbol, forever, with the analytics pipeline permanently non-functional and no startup-time signal.
- Recommended fix: Add `Field(gt=0)` (or `ge=2` where a minimum window makes sense) to all period/window settings in `Settings`.
- Effort: S.

**2.7 — [MEDIUM] CSV backfill loader: one bad row aborts the whole file, no row-context, no transaction**
- Location: `src/orvixa/backfill/csv_loader.py:33-36` (`_parse_ts`), `:39-52` (`_row_to_candle_row`), `:55-85` (`load_candles_csv`).
- A malformed `ts` (empty, non-numeric, non-ISO) raises a bare `ValueError: Invalid isoformat string: '...'`; a missing/typo'd CSV column raises `KeyError`; a non-numeric OHLCV cell raises `ValueError` from `float()`/`int()`. None carry filename/row-number context. Batches already flushed via `insert_batch` before the failing row remain committed (idempotent upserts mean a re-run is safe, but there's no transaction wrapping the whole file, so a partial load is the actual on-disk state until someone notices and re-runs).
- No OHLC sanity checks (`high >= low`, `high >= max(open,close)`, `volume >= 0`).
- Recommended fix: Wrap per-row parsing in try/except with row-number context; add OHLC sanity validation; consider a `--strict`/`--skip-bad-rows` mode.
- Effort: S-M. (Acknowledged as CLI-only / "frozen data layer" per `DATASET.md`, so lower urgency, but still a real gap if used operationally for real-data backfills.)

**2.8 — [LOW] Idempotency is solid where it matters**
- `CandleRepository.insert_batch` (`db/repository.py:99-140`) and `IndicatorRepository.upsert`/`upsert_batch` (lines 192-257) both use `ON CONFLICT ... DO UPDATE` on the natural key — confirmed correct, confirmed tested (`test_repository.py`, opt-in `test_db_integration.py:73-81`). `telegram_alerts` has a partial unique index on `dedupe_key` backing `ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING` (`repository.py:414`) — correct, though unused (see Section 9/10).
- `SymbolRepository.update_ranking` (`repository.py:83-90`) discards the `execute()` result status — silently no-ops if `base` doesn't exist in `symbols`. Low severity (the row should always exist by the time ranking is updated, given `upsert` is called first in `_persist`), but worth a comment/assertion for defensive clarity. Effort: S.

---

## 3. Database

### Schema quality

- 8 tables created in `alembic/versions/0001_initial_schema.py` (205 lines): `symbols`, `candles` (hypertable), `indicators` (hypertable), `signals`, `market_events`, `market_memory`, `market_reports`, `telegram_alerts`. `0002_symbol_ranking.py` (36 lines) additively adds `rank smallint`, `metrics jsonb DEFAULT '{}'`, `last_synced timestamptz` to `symbols`.
- `candles` PK `(symbol_id, interval, ts)`, `indicators` PK `(symbol_id, ts)` — correct natural keys for upsert-based idempotency.
- Indexes present: `signals_symbol_ts_idx`, `market_events_symbol_ts_idx`, `market_memory_ts_idx`, `market_reports_ts_idx`, `telegram_alerts_ts_idx`, partial unique `telegram_alerts_dedupe_key_uniq`.
- All raw-SQL migrations use `op.execute()` with fully static strings — **no SQL injection risk in migrations** (confirmed).

### Findings

**3.1 — [HIGH] TimescaleDB hypertable/compression DDL has never been executed against real TimescaleDB**
- Location: `alembic/versions/0001_initial_schema.py:66,76,95,105` (`create_hypertable`, `add_compression_policy('... 7 days')` for `candles` and `indicators`); `M2_VALIDATION_REPORT.md` lines 16-22, 53-69, 192-193.
- The team's own validation report documents that Docker Hub rate-limiting prevented pulling `timescale/timescaledb:2.17.2-pg16`, and the TimescaleDB extension itself returned 403 from `packagecloud.io` — so the M2 "live-DB smoke test" that gated the GO decision for M3 ran against **plain Postgres 16 without the TimescaleDB extension at all**. The hypertable/compression calls were validated only as "syntactically valid SQL", never executed.
- Business impact: **The single most distinctive infrastructure claim of M2 — "TimescaleDB hypertables with 7-day compression for candles/indicators" — is unverified.** If `create_hypertable`/`add_compression_policy` fail or behave unexpectedly against the real image referenced in both compose files, `alembic upgrade head` could fail on first deploy, or (worse) silently no-op leaving `candles`/`indicators` as plain unpartitioned tables that will degrade badly at scale (one row per symbol per minute, indefinitely, per Section 3.2).
- Recommended fix: Run `alembic upgrade head` against the actual `timescale/timescaledb:2.17.2-pg16` image (the compose files already reference it) in CI, and assert `SELECT * FROM timescaledb_information.hypertables` and `...compression_settings` return the expected rows.
- Effort: S (this is a CI/validation task, not a code change) but **blocking** — everything else about the data layer's scalability assumes this works.

**3.2 — [HIGH] No retention policy anywhere, and no continuous aggregates**
- Location: `alembic/versions/0001_initial_schema.py` — `add_compression_policy` exists for `candles`/`indicators` (lines 76, 105) but there is **no `add_retention_policy`** call anywhere, for any table.
- `signals`, `market_events`, `market_memory`, `market_reports`, `telegram_alerts` are plain (non-hypertable) Postgres tables with no partitioning and no pruning strategy — they grow unbounded forever.
- No continuous aggregates exist for hourly/daily OHLC rollups. Every query against `candles`/`indicators` (including the `/signals/{symbol}` endpoint's underlying joins, and any future `/candles` endpoint) is a raw row scan. At 518,400 rows for the demo dataset alone (12 symbols × 43,200 1m candles), and growing per-symbol-per-minute in production across potentially hundreds of Tier 0/1/2 symbols, this is a real scaling cliff with nothing to fall back on.
- Recommended fix: Add `add_retention_policy` for `candles`/`indicators` (e.g., drop chunks older than N months, or convert to a tiered storage policy), and add at least one continuous aggregate (e.g., 1h OHLC rollup) for dashboard queries. Convert `signals`/`market_events`/`market_memory` to hypertables with their own retention policies.
- Effort: M.

**3.3 — [MEDIUM] `0001_initial_schema.py` `downgrade()` is unconditionally destructive**
- Location: `alembic/versions/0001_initial_schema.py:197-205` — `DROP TABLE IF EXISTS` for all 8 tables, no guard, no warning, no backup step.
- Expected for an "initial" migration in isolation, but there is **no tooling-level safeguard** (no confirmation prompt, no environment check) preventing `alembic downgrade base` from being run against a populated production database and permanently deleting all history. `0002`'s downgrade (drop 3 added columns) is correctly scoped/additive-only and is fine.
- Recommended fix: Add an operational runbook note and/or a pre-downgrade check (e.g., refuse to run `downgrade base` if `APP_ENV=production` without an explicit override flag).
- Effort: S.

**3.4 — [LOW] All OHLCV/indicator numeric columns use unbounded `numeric` (no precision/scale)**
- Location: `alembic/versions/0001_initial_schema.py` lines 54-61 (`o,h,l,c,v,quote_v,taker_buy_v`), 84-90 (`ema_fast`...`trend_score`), 132/134 (`magnitude`/`price`), 149 (`breadth`).
- Self-flagged in `M2_VALIDATION_REPORT.md` issue #4 as "cosmetic... not fixed". Agreed it's not a correctness bug, but unbounded `numeric` has real storage and computation cost vs. `numeric(18,8)` or `double precision`, with no tracked follow-up.
- Effort: M (requires a migration + careful handling of any existing data).

**3.5 — [LOW] `symbols` table has no index to support `ORDER BY rank`**
- Location: `alembic/versions/0002_symbol_ranking.py:28-30` adds `rank`, `metrics`, `last_synced` with no index.
- Low impact at current scale (~12-600 rows), but `SymbolRepository.update_ranking` exists specifically to support ranking queries that don't yet exist in the API — worth an index if/when a "top-ranked symbols" endpoint ships.
- Effort: S.

**3.6 — [INFO] `env.py`'s DSN rewrite is fragile**
- Location: `alembic/env.py:30-34` — naive `postgresql://` → `postgresql+asyncpg://` string-prefix rewrite. A `postgres://...` DSN (a very common alternate scheme) silently fails to get an async driver. Not currently exercised by any test or config default. Effort: S.

---

## 4. Performance

### Findings

**4.1 — [HIGH] `SymbolManager._persist` is a sequential per-symbol loop, including frozen/delisted symbols, holding the refresh lock for its entire duration**
- Location: `src/orvixa/symbols/manager.py:211-212` (`for state in self._states.values(): await self._persist(state)`), `:353-372` (`_persist` — two sequential round trips: `upsert` then `update_ranking`).
- Self-acknowledged in `M3_VALIDATION_REPORT_FINAL.md` §3.3 as ~3ms/symbol × 2 queries, extrapolated to **15-35 seconds per refresh cycle** for the full Binance USDT universe (400-600 pairs), against a non-local DB — "reported, not fixed".
- Compounding: `_states` is monotonically growing — `_assign_tiers` skips `frozen` symbols (line 276) but `_persist`'s loop does **not** (no filter at lines 211-212), so every delisted symbol pays its 2-round-trip cost forever, every cycle, with `last_synced` updated for symbols that will never trade again.
- The entire `refresh_universe()` body — including this loop — runs under `self._refresh_lock` (line 199). With `symbol_refresh_interval_seconds` defaulting to 300s, 15-35s is "under budget" today but is a real, growing fraction of the cycle, and the lock would block any future API-triggered "refresh now" feature for its full duration.
- Recommended fix: (a) batch the upsert+ranking writes (e.g., a single multi-row `INSERT ... ON CONFLICT` via `executemany` or a VALUES-list query), (b) exclude `frozen` symbols from the persistence loop (or persist them far less frequently), (c) consider pruning `_states`/`symbols` rows for symbols frozen longer than some retention window.
- Effort: M.

**4.2 — [MEDIUM] `_rank_universe` computes `rank_by_score` twice per cycle (full universe + alts-only)**
- Location: `src/orvixa/symbols/manager.py:222-240`. Two O(n log n) sorts over up to 600 items every cycle — cheap individually but unnecessary; the alt-only ranking is a strict subset that could be derived via filtering the full ranking.
- Effort: S. Severity low/medium given current scale.

**4.3 — [MEDIUM] `BinanceMarketClient` opens a fresh `httpx.AsyncClient` (new TCP+TLS) per call, twice per refresh cycle**
- Location: `src/orvixa/symbols/client.py:60,86` (`fetch_exchange_info`, `fetch_ticker_24hr`), each via `self._client_factory()`.
- At a 300s cadence this is 2 new connections every 5 minutes — not severe, but a missed opportunity for connection reuse, and inconsistent with `BinanceFeed._default_backfiller` which at least reuses one client across its per-symbol loop.
- Effort: S.

**4.4 — [MEDIUM] Signals and events are persisted one row at a time (no batching), unlike indicators**
- Location: `src/orvixa/analytics/engine.py` ~lines 172, 186 — individual `await self._signal_repo.insert(...)` / `await self._event_repo.insert(...)`, each a separate `fetchrow ... RETURNING id` round trip (`db/repository.py:278-289, 314-325`), vs. `IndicatorRow` which is queued to a `BatchWriter` (line ~157).
- The M4 delivery report frames §3 as "Performance analysis (hundreds of symbols efficiently)" while only indicators are actually batched — signals/events are explicitly justified as "rare enough not to matter", which holds under calm conditions but means **a market-wide event burst (flash crash triggering simultaneous vol_spike/pump/dump/breakdown across many symbols) produces many sequential synchronous DB round trips with no batching**, and this path was never exercised in the M4 150k-candle benchmark (which reported `signals_emitted=0`).
- Recommended fix: Add a `BatchWriter` for signals/events mirroring the indicator path, or at minimum benchmark the burst scenario.
- Effort: M.

**4.5 — [LOW] `BinanceFeed._default_backfiller` issues sequential per-symbol REST calls on every reconnect**
- Already covered under 2.5 — repeated here for the performance angle: 20-30 sequential `/api/v3/klines` round trips on every reconnect both delays gap-fill completion and risks tripping Binance's IP-based REST weight limits if reconnects are frequent (a "reconnect storm" amplifier). Effort: M (parallelize with `asyncio.gather` + concurrency cap).

**4.6 — [INFO] Memory is genuinely bounded and was load-tested**
- The M4 validation report's 150k-candle/300-symbol benchmark (`~169µs/candle`, `~9.5 MiB` peak via `tracemalloc`) is credible given the code: `AnalyticsEngine`'s per-symbol dicts (`_indicators`, `_latest_trend`, `_symbol_ids`), `BreadthEngine._history`, and `EventEngine`'s bounded deques are all bounded by *symbol count*, not *candle count* — confirmed by direct read of `analytics/engine.py`, `symbols/breadth.py`, `analytics/events.py`. No unbounded growth found in the hot path. Good.

---

## 5. Security

### Findings

**5.1 — [HIGH] `app_env` is an unconstrained string — every production safeguard hinges on an exact `.lower() == "production"` match**
- Location: `src/orvixa/config.py:120` (`app_env: str = "production"`), and the three security validators that gate on it: `_validate_cors_origins` (135-143), `_validate_api_key` (145-153), `_validate_no_default_credentials` (155-164).
- A typo (`APP_ENV=Production ` with trailing whitespace, `APP_ENV=prod`, `APP_ENV=PRODUCTION` handled by `.lower()` but anything else not) silently falls through to **dev-mode behavior with no warning**: CORS wildcard permitted, empty/default API key permitted, `orvixa:orvixa` default credentials permitted. This is the single highest-leverage misconfiguration in the codebase — one bad env var line disables three independent security controls simultaneously, silently.
- Recommended fix: Use a `Literal["development", "staging", "production"]` (or an `Enum`) for `app_env` so pydantic rejects unrecognized values at startup, and/or invert the logic to "deny by default" (require an explicit `app_env == "development"` to *relax* controls, rather than requiring an exact match to *enable* them).
- Effort: S.

**5.2 — [MEDIUM] Confirmed: `MarketReportRepository.insert` jsonb bug (cross-referenced with 2.3)** — see Section 2.3.

**5.3 — [LOW] Non-constant-time API key comparison**
- Location: `src/orvixa/api/auth.py:29` — `if x_api_key != expected:`.
- A textbook timing side-channel (Python `!=` short-circuits on first mismatched byte). Low-but-nonzero severity for a single shared secret protecting an API whose value is in data freshness/aggregation — exactly the kind of asset worth scraping.
- Recommended fix: `hmac.compare_digest(x_api_key or "", expected)`.
- Effort: S (one line).

**5.4 — [LOW] Empty API key fully disables auth — enforcement depends entirely on the fragile `app_env` check (5.1)**
- Location: `src/orvixa/api/auth.py:27-28` (`if not expected: return`), guarded only by `config.py:145-153`'s production check.
- Effort: combine with 5.1's fix.

**5.5 — [MEDIUM] Production runs the dev Dockerfile — dev tooling shipped into prod image, runs as root**
- Location: `docker-compose.prod.yml:9` (`dockerfile: Dockerfile.dev`); `Dockerfile.dev:12` (`pip install -e ".[dev,api]"`); no `USER` directive anywhere.
- `pytest`, `ruff`, `mypy`, `sqlalchemy[asyncio]`, `alembic` — all dev-only per `pyproject.toml`'s `[project.optional-dependencies]` split — are installed into the production image, increasing both image size and CVE attack surface for packages that provide zero runtime value. The container runs all processes (API, ingest, analytics daemons, migrations) as root.
- Recommended fix: Create a `Dockerfile.prod` (multi-stage: build stage installs `.[api]` only into a venv, final stage copies the venv + `src` into a slim runtime image with a non-root `USER`).
- Effort: M.

**5.6 — [MEDIUM] No dependency lockfile — unbounded `>=` ranges on every dependency**
- Location: `pyproject.toml:12-31` — `pydantic>=2.7`, `fastapi>=0.111`, `asyncpg>=0.29`, `websockets>=12.0`, `httpx>=0.27`, `sqlalchemy[asyncio]>=2.0`, etc., all floor-only, no upper bounds. No `requirements*.txt`, `poetry.lock`, `uv.lock`, or `Pipfile.lock` anywhere.
- Business impact: Reproducible builds are not possible. A `docker build` today vs. a year from now can resolve materially different major versions of `pydantic`/`fastapi`/`sqlalchemy` with no warning — a classic "works today, breaks on rebuild" risk, and makes CVE/SBOM analysis of "what's actually deployed" impossible without first generating a lockfile.
- Recommended fix: Adopt `uv`/`poetry`/`pip-tools` and commit a lockfile; pin `requires-python` with an upper bound too (`>=3.11,<3.13`).
- Effort: M.

**5.7 — [LOW] Hardcoded dev credentials checked into source control**
- Location: `docker-compose.dev.yml:25` (`POSTGRES_PASSWORD: orvixa`), `.env.example:45` (`POSTGRES_DSN=postgresql://orvixa:orvixa@postgres:5432/orvixa`), `REPRODUCIBILITY_NOTES.md:35` (documents creating a Postgres **SUPERUSER** named `orvixa` with password `orvixa`).
- Mitigated by `config.py:155-164`'s `_validate_no_default_credentials` (rejects `"orvixa:orvixa"` substring in production) — but that mitigation itself depends on the fragile `app_env` check (5.1). The `REPRODUCIBILITY_NOTES.md` superuser instruction, even if dev-only, is a bad habit checked into the repo for anyone following it as a template.
- Effort: S (document more prominently that this is dev-only; consider a stronger generated default even for dev via `bootstrap-env.sh`).

**5.8 — [LOW] `bootstrap-env.sh` does not `chmod 600` the generated `.env`**
- Location: `scripts/bootstrap-env.sh` (41 lines) — generates `POSTGRES_PASSWORD`/`REDIS_PASSWORD`/`API_KEY` via `openssl rand -hex`, writes to a `.env` file, but never restricts its permissions (defaults to umask, typically 644 / world-readable on multi-user hosts).
- Recommended fix: Add `chmod 600 "$OUT"` after generation.
- Effort: S (one line).

**5.9 — [INFO] No SQL injection risk found anywhere**
- Confirmed across `db/repository.py` (all 8 repository classes use asyncpg positional parameters `$1, $2, ...`) and `alembic/` migrations (static `op.execute()` strings, no interpolation). This is a genuine strength.

**5.10 — [MEDIUM] Frontend stores API key in `localStorage` and renders unescaped `innerHTML` from API data**
- Location: `frontend/index.html:190,192` (localStorage), and `renderRegime`/`renderPolicy`/`renderSignals` (~lines 125-159) build HTML via template literals assigned to `innerHTML`.
- `localStorage` is accessible to any script on the page (XSS pivot), and the unescaped `innerHTML` rendering is itself a stored-XSS sink if any upstream data field (signal payload, regime tags, etc.) ever contains HTML/script content — currently all data is internally generated (synthetic or normalized exchange data), but there's no defense-in-depth if that assumption ever breaks (e.g., a future user-submitted symbol alias, or a compromised upstream feed).
- `renderSymbolPicker` correctly uses `createElement`/`textContent` — so the codebase knows the safe pattern, it's just inconsistently applied.
- Recommended fix: Use `textContent`/`createElement` consistently, or escape interpolated values; consider a short-lived session token instead of a persistent API key in `localStorage`.
- Effort: S-M.

---

## 6. API Readiness

### Findings

**6.1 — [HIGH] No API versioning**
- Location: `src/orvixa/api/app.py` — every route (`/health`, `/symbols`, `/signals/{symbol}`, `/regime/{symbol}`, `/policy/{symbol}`) is unprefixed, no `/v1/`, no `APIRouter` with a version.
- Business impact: Any breaking response-shape change has no migration path for existing clients (including the bundled frontend and any future Telegram bot / external integrations).
- Recommended fix: Prefix all routes with `/v1`, structure for future `/v2` via a versioned `APIRouter`.
- Effort: S (mostly mechanical, but touches the frontend's hardcoded paths too).

**6.2 — [HIGH] Zero Pydantic response models — endpoints return raw `dict(asyncpg.Record)`**
- Location: `src/orvixa/api/app.py` (all 5 routes return `dict`/`list[dict]` via `record_to_dict`/`records_to_list`, `src/orvixa/api/deps.py:35-40`).
- Business impact: The auto-generated OpenAPI schema (`/docs`) documents every response as `{}`/`[{}]` — no field names, types, or structure, making the API effectively undocumented for any external consumer. There is also **no response validation** — if a column is renamed/added/removed in a migration, the API silently changes shape with zero compile-time or runtime contract enforcement.
- `/symbols` returns the entire `symbols` table verbatim (`SELECT * FROM symbols ORDER BY id`, `repository.py:81`) including internal columns (`tags`, `metrics`, `last_synced`, `rank`) with no field allowlist — any future internal-only column automatically becomes public API surface with zero review gate.
- Recommended fix: Define Pydantic response models for each endpoint (`SymbolOut`, `SignalOut`, `RegimeOut`, `PolicyOut`) with explicit field lists; use `response_model=` on each route.
- Effort: M.

**6.3 — [MEDIUM] No pagination on list endpoints**
- Location: `/symbols` (`SymbolRepository.list_all()` — `SELECT * FROM symbols ORDER BY id`, **no LIMIT at all**); `/signals/{symbol}` capped at `api_signals_limit` (default 50, `config.py`) with **no offset/cursor** — clients cannot retrieve historical signals beyond the most recent 50.
- `/symbols` is currently bounded by the small symbol universe (~12, growing to maybe hundreds if M3 is ever deployed per finding 1.1) — no defensive cap today.
- Recommended fix: Add `limit`/`offset` (or cursor-based) query params to both endpoints, with sane defaults and max caps.
- Effort: S-M.

**6.4 — [MEDIUM] No custom exception handlers — generic 500s, no correlation IDs**
- Location: `src/orvixa/api/app.py` — no `@app.exception_handler(...)` registered. Any unhandled exception (DB connection drop, the jsonb bug from 2.3, a malformed jsonb decode) returns FastAPI's default `{"detail": "Internal Server Error"}` with no structured logging tying the error back to a request/correlation ID.
- Recommended fix: Add a global exception handler that logs via `orvixa.logging`'s JSON formatter with a request ID, and returns a consistent error envelope.
- Effort: S-M.

**6.5 — [MEDIUM] No rate limiting**
- A single shared static `X-API-Key` with no per-key/per-IP rate limit (no `slowapi` or equivalent). One leaked key (or a single misbehaving client) can hammer `/signals/{symbol}` without limit.
- Effort: S-M (e.g., `slowapi` + Redis, which is already in the stack).

**6.6 — [MEDIUM] `app = create_app()` at module scope — import has production-config side effects**
- Location: `src/orvixa/api/app.py:122`.
- Importing `orvixa.api.app` calls `get_settings()` and `setup_logging()`, constructing the full `Settings` object — which, per `config.py:147-164`, **raises `RuntimeError`** if `APP_ENV=production` and `API_KEY`/DSNs are still defaults. `tests/conftest.py` works around this with `os.environ.setdefault("APP_ENV", "development")` *before* any test module imports `orvixa.api.app` — fragile (any tool/import ordering that bypasses conftest crashes on import in a misconfigured env). Necessary for uvicorn's `module:app` target, but couples import-time to runtime-config validation.
- Recommended fix: Move the module-level `app = create_app()` into a small `if __name__` / lazy-factory pattern, or accept this as a documented constraint with a clearer error message.
- Effort: S.

**6.7 — [LOW] `/regime/{symbol}` and `/policy/{symbol}` both perform a wasted symbol-existence DB round trip**
- Location: `src/orvixa/api/app.py` (~lines 106, 116) call `_symbol_id_or_404` purely for 404 validation even though `market_memory` has no `symbol_id` (whole-market) and `/policy` always returns `{}`.
- Effort: S (informational/minor).

**6.8 — [INFO] Confirmed correct behaviors**
- `/health` correctly unauthenticated (`test_health_needs_no_auth`); auth correctly required on the other 4 routes (`test_missing_api_key_is_rejected`); 404 on unknown symbol (`test_unknown_symbol_is_404`); `/regime`/`/policy` correctly return `{}` placeholders matching documented M5 scope; CORS `allow_methods=["GET"]` is appropriately restrictive for a read-only API. The API is genuinely read-only — confirmed only `MarketMemoryRepository`, `SignalRepository`, `SymbolRepository` are imported, and every method called issues only `SELECT`.

**6.9 — No WebSocket API exists.** The dashboard polls every 7s (`frontend/index.html`, `POLL_MS = 7000`) — not a defect per se for a read-only/low-frequency dashboard, but worth noting if "real-time" is ever a marketed feature.

---

## 7. Frontend Readiness

### Findings

**7.1 — [MEDIUM] Single static HTML file, no build system, no framework**
- Location: `frontend/index.html` (200 lines) — vanilla JS, inline CSS/JS, dark theme.
- Maintainability: fine for a 200-line dashboard; will not scale past "internal ops dashboard" without a real frontend toolchain (bundler, component framework, test harness — none exist).

**7.2 — [MEDIUM] localStorage API key + unescaped `innerHTML`** — see 5.10.

**7.3 — [MEDIUM] No accessibility considerations**
- No ARIA attributes, no `aria-live` regions for the polling-updated panels (signals/regime/policy), color-only status indication (a common a11y failure for dashboards with "healthy/unhealthy" style indicators).
- Effort: S-M to add `aria-live="polite"` to update regions and non-color status indicators (icons/text).

**7.4 — [LOW] Polling-based, not WebSocket** — see 6.9. 7s polling of 3 endpoints per active client is fine at small scale but doesn't scale to many concurrent dashboard users without the rate-limiting from 6.5.

**7.5 — [INFO] `renderSymbolPicker` correctly uses safe DOM APIs** (`createElement`/`textContent`) — shows the team knows the safe pattern; the inconsistency in 5.10 is the actual issue.

---

## 8. Testing

### Coverage summary
- 24 test files, 165 test functions, **164 passed / 1 skipped** — independently re-run via `/tmp/venv/bin/python -m pytest -q` during this audit and confirmed accurate as of the current `tests/` tree.
- Strong coverage: feed reconnect/backoff/resubscribe/gap-fill (`test_reconnect.py`, all via injected fakes, no network); repository upserts and idempotency (`test_repository.py`, opt-in `test_db_integration.py`); signal/event/regime engines including the M3/M4 documented bug-fix regressions; backtest/validation modules (`signal_validation.py`, `regime_validation.py`, `policy_validation.py`) — the most thoroughly tested code in the repo, including determinism tests for bootstrap CIs.

### Findings

**8.1 — [HIGH] Zero test coverage for `runners/daemon.py::supervise` and all 6 CLI runners**
- Location: `src/orvixa/runners/{ingest,analytics,symbols,backfill,daemon,feedcheck}.py` — no `test_runners*.py`, `test_daemon.py`, `test_ingest.py`, etc. exist (`test_backfill_csv_loader.py` tests the *library function*, not `runners/backfill.py`'s CLI wrapper).
- Business impact: `daemon.py::supervise` is the entire "Phase 2 keep-services-alive" mechanism — restart-on-crash, restart-on-clean-exit, SIGINT/SIGTERM handling. A regression here (e.g., an exception-handling change that stops restarts, or a signal-handler bug that prevents graceful shutdown) would not be caught by `make test` and would only surface in production as "the ingest daemon stopped and didn't come back."
- Recommended fix: At minimum, a unit test for `supervise()` using a fake `run()` that raises N times then succeeds, asserting restart count and interval behavior; a test for signal-handler registration/graceful shutdown.
- Effort: M.

**8.2 — [HIGH] `MarketReportRepository.insert`'s jsonb bug (2.3) is invisible to the test suite by construction**
- `FakePool` (`tests/test_repository.py`) performs no serialization, so the missing `json.dumps`/`::jsonb` is undetectable without a real (or serialization-aware) pool. `test_db_integration.py` covers only `CandleRepository`/`SymbolRepository`.
- Recommended fix: extend `test_db_integration.py` (or `FakePool`) to round-trip jsonb fields for all 4 repositories that write jsonb columns.
- Effort: S.

**8.3 — [MEDIUM] No test exercises `_default_connector`/`_default_backfiller` (real `websockets`/`httpx` code paths)**
- Location: `src/orvixa/feeds/binance.py:266-285` — every test injects fake `connector`/`backfiller`. The actual `websockets.connect(url, ping_interval=20, ping_timeout=20)` call and the actual `/api/v3/klines` REST request construction (params, `raise_for_status`, per-symbol exception handling) have zero coverage. Combined with the 3 mypy errors in this file (Protocol mismatches), this is the least-validated "real" code path in the feed layer.
- Effort: M (requires either a recorded-response test harness or a live-network opt-in test).

**8.4 — [MEDIUM] `_MAX_STREAMS_PER_SOCKET` (1000) warning-only path is untested**
- No test subscribes >1000 symbols to confirm the warning fires and that nothing else (sharding) happens — confirming the documented "no sharding" gap (1.1-adjacent finding from feeds agent) is itself unverified by test.
- Effort: S.

**8.5 — [MEDIUM] Symbol-collision scenario (1.4) has no regression test**
- `M3_VALIDATION_REPORT_FINAL.md` claims it was "reproduced with a synthetic two-pair fixture during the audit" but that fixture was never committed — confirmed absent from `test_symbol_manager.py`.
- Effort: S (write the fixture that was reportedly already created once).

**8.6 — [MEDIUM] `AnalyticsEngine.start()`/`stop()`/`_loop()` periodic path untested end-to-end**
- `test_analytics_engine.py` calls `refresh_regime()` directly but never `engine.start()` — the `_loop`'s sleep→refresh cycle, `CancelledError` handling, and `stop()`'s suppression are untested. Same gap applies to `SymbolManager._loop`.
- Effort: M.

**8.7 — [MEDIUM] `BatchWriter`'s data-loss-on-error behavior tested only for the error counter, not the data loss itself**
- `test_sink_error_is_isolated_and_counted` confirms `error_count` increments but doesn't assert that the failed batch's items are gone/unretried — i.e., the *symptom* is tested, not the *implication* (2.1).
- Effort: S.

**8.8 — [MEDIUM] CSV loader edge cases untested**
- Missing columns, malformed numerics, empty files, partial-failure-mid-file — none covered (2.7).
- Effort: S-M.

**8.9 — [INFO] Test-count progression is honest and verifiable**
- 20 → 42 → 69 → 107 → 112 → 164 across M1→M2→M3→M4→(M4 fixes)→M5, each with a corresponding validation report describing what was added/fixed. This is a genuinely good engineering practice — most of the audit's "self-reported" claims about test counts and bug fixes were independently verified as accurate.

---

## 9. DevOps

### Findings

**9.1 — [CRITICAL] No production Dockerfile — `docker-compose.prod.yml` builds from `Dockerfile.dev`**
- Location: `docker-compose.prod.yml:9` (`dockerfile: Dockerfile.dev`); confirmed via `ls *Dockerfile*` — only `Dockerfile.dev` exists.
- See 5.5 for the security angle. From a pure DevOps standpoint: there is no image-hygiene distinction between "what a developer runs locally" and "what serves real traffic" — same layers, same installed packages, same root user, no multi-stage build, no `HEALTHCHECK` directive in the image itself (healthchecks exist only at the compose level).
- Effort: M.

**9.2 — [HIGH] `orvixa-symbols` (M3) absent from both compose files** — see 1.1.

**9.3 — [MEDIUM] No resource limits anywhere**
- Neither compose file sets `deploy.resources.limits`, `mem_limit`, or `cpus` for any service. A runaway `ingest`/`analytics` daemon (e.g., from the unbounded-`_states`-growth scenario in 4.1, or a batch-writer buffer that grows if `_flush` is somehow blocked) has no host-level cap.
- Effort: S.

**9.4 — [MEDIUM] Dev/prod compose differences are otherwise genuinely good**
- Prod hardens via: required (non-default) env vars using Compose `:?` syntax for DB/Redis credentials and API key; Redis `--requirepass` + auth'd healthcheck; services bound to `127.0.0.1` only (`api`, `frontend`) with a comment indicating a reverse proxy should terminate TLS; `restart: unless-stopped`; `json-file` logging with rotation via a shared `x-app`/`logging` anchor; `frontend` waits on `api`'s `service_healthy` condition. This is a better-than-average split for a project this size — the gap is entirely that both still build the *same image*.
- `backfill` profile exists in dev only — no documented production data-loading mechanism (acceptable if backfill is a one-time/rare operation run manually, but undocumented).

**9.5 — [MEDIUM] Makefile is dev-only — no `make` targets for prod deploy, backup, restore, or secret rotation**
- Location: `Makefile` (36 lines) — `dev`, `build`, `down` (which does `-v`, **deleting volumes** — i.e. `make down` during dev wipes the database), `feedcheck`, `ingest`, `migrate`, `test`, `fmt`, `lint`. No `make prod`/`make deploy`/`make backup`.
- All production operations (`bootstrap-env.sh`, `docker-compose -f docker-compose.prod.yml ...`, any backup/restore) are entirely manual and outside the Makefile — high risk of operator error during an actual incident.
- Effort: S-M (add `make prod-up`, `make backup-db`, `make restore-db` targets at minimum).

**9.6 — [HIGH] No backup strategy at all**
- No backup job, no `pg_dump` cron, no volume-snapshot automation, no documented restore procedure anywhere in compose files, scripts, or Makefile. For a system whose entire value is its accumulated time-series data (and whose compression/retention story is itself unproven per 3.1/3.2), this is a significant operational gap.
- Effort: M (e.g., a sidecar `pg_dump` cron container + offsite storage, documented restore runbook).

**9.7 — [LOW] Layer caching in `Dockerfile.dev` invalidates the pip-install layer on any source change**
- `COPY src ./src` happens before `pip install -e ".[dev,api]"` (lines 10-12) — every code change re-triggers the full dependency install (slow, and combined with `PIP_NO_CACHE_DIR=1`, every build re-downloads everything with no BuildKit cache mount).
- Effort: S (reorder COPY/install steps, add a cache mount).

---

## 10. Observability

### Findings

**10.1 — [HIGH] No metrics endpoint, no Prometheus/Grafana/OpenTelemetry anywhere**
- Repo-wide grep for `prometheus`/`grafana`/`opentelemetry` returns zero matches. `/health` is the only operational endpoint, returning a static `{"status": "ok"}` with **no DB connectivity check** — a database outage would not be reflected in `/health`, meaning the compose healthcheck (and any load-balancer relying on it) would report the API as healthy while every real query fails.
- All operational counters (`flush_count`, `error_count` from `BatchWriter`; `refresh_count`, `last_tier_changes` from `SymbolManager`; `regime_refresh_count`, `connect_count`, `gapfill_count`, `backoff_history`, `resubscribe_count` from `BinanceFeed`/`AnalyticsEngine`) exist as in-memory attributes but are **only visible via JSON log lines** — none are queryable, none back a dashboard, none can trigger an alert.
- Recommended fix: Add a `/metrics` Prometheus endpoint (or push to a metrics backend) exposing at minimum: batch-writer error/drop counts, feed connect/reconnect/gapfill counts, regime-refresh staleness, symbol-manager refresh duration and tier-change counts. Extend `/health` to check DB connectivity (a cheap `SELECT 1`).
- Effort: M.

**10.2 — [HIGH] `telegram_alerts` table exists with zero implementation — alerting is non-functional**
- Location: schema fully defined (`alembic/versions/0001_initial_schema.py:175-194`, including the dedupe-key partial unique index), `TelegramAlertRepository`/`TelegramAlertRow` exist in `db/repository.py`/`db/models.py`. But there is **no Telegram bot token, webhook URL, or any alert-sending code** anywhere in `.env.example`, `config.py`, or any runner.
- Business impact: The schema strongly implies "alerting is delivered" (it's reserved as M7 per the milestone numbering), but as of this audit it is purely dormant infrastructure — no alert has ever been or can ever be sent. Combined with 10.1 and 2.1/2.2 (silent batch-writer drops, silent regime-refresh stalls), **the system currently has no mechanism to notify anyone of any failure mode**, despite having a database table that looks like it should.
- Recommended fix: scope and implement M7, or at minimum document clearly that `telegram_alerts` is schema-only and not yet a working feature (so it isn't mistaken for a safety net).
- Effort: L (full M7 implementation) / S (documentation-only interim fix).

**10.3 — [MEDIUM] Structured JSON logging is solid but has no log-level escalation path**
- Location: `src/orvixa/logging.py` (58 lines) — custom `JsonFormatter` (correct, includes `ts`/`level`/`logger`/`msg`/`extra`/`exc`), `setup_logging()` quiets `websockets`/`httpx`/`httpcore` to WARNING. Pairs with `docker-compose.prod.yml`'s `json-file` log driver + rotation.
- Gap: this is "logs you could ship to ELK/Loki if you set that up" — but nothing in the repo actually ships them anywhere, and several of the most important failure signals identified in this audit (2.1's batch-writer drops, 2.2's silent regime stalls, 10.2's non-functional alerting) currently rely entirely on a human reading these JSON lines in real time or grepping after the fact.
- Effort: M (log shipping + alerting rules are an infra task, not a code task, but the code-side counters needed to make alerting meaningful are partially missing per 10.1).

**10.4 — [INFO] `signal_validation.py` uses `print()` instead of the structured logger for dataset-provenance warnings**
- Location: `src/orvixa/backtest/signal_validation.py` ~lines 318, 327-330 — `print(f"[signal_validation] dataset: {dataset_type} | mode: {mode}")` and a SYNTHETIC-data warning, both via `print()` rather than `orvixa.logging`'s logger.
- For something as consequential as "did we just refuse an edge_evaluation run because the dataset was synthetic", this should be a WARNING-level structured log line, not stdout text that could be missed in an automated nightly job.
- Effort: S.

---

## Production Checklist

**Blocking (must fix before any real-money / real-user launch):**
- [ ] Run `alembic upgrade head` against the actual `timescale/timescaledb:2.17.2-pg16` image in CI and verify hypertables/compression actually exist (3.1)
- [ ] Fix `MarketReportRepository.insert` jsonb serialization (2.3) before M6 ships
- [ ] Build and use a real `Dockerfile.prod` (multi-stage, `.[api]` only, non-root user) (5.5, 9.1)
- [ ] Generate and commit a dependency lockfile; pin `requires-python` upper bound (5.6)
- [ ] Fix `app_env` to a constrained `Literal`/`Enum` so production safeguards can't be silently bypassed by typos (5.1)
- [ ] Add `try/except` around `SymbolManager._loop` and `AnalyticsEngine._loop` cycle bodies (1.2)
- [ ] Fix `BatchWriter._flush`'s data-loss-on-error: retry + dead-letter or re-buffer (2.1)
- [ ] Add `Field(gt=0)` validation to all period/window settings in `config.py` (2.6)
- [ ] Decide M3 deployment: either add `orvixa-symbols` to compose or formally descope dynamic discovery for v1 (1.1)
- [ ] Add a backup/restore strategy for Postgres/TimescaleDB (9.6)
- [ ] Add a `/metrics` endpoint and DB-connectivity check in `/health` (10.1)
- [ ] Resolve or formally descope `telegram_alerts`/M7 — don't ship a dormant alerting table that looks functional (10.2)

**High priority (fix before broad/public launch):**
- [ ] Add Pydantic response models + API versioning (`/v1`) (6.1, 6.2)
- [ ] Add pagination to `/symbols` and `/signals/{symbol}` (6.3)
- [ ] Add rate limiting (6.5)
- [ ] Add retention policy + at least one continuous aggregate (3.2)
- [ ] Fix `hmac.compare_digest` for API key check (5.3)
- [ ] Add application-level WS liveness watchdog + gap-fill retry/parallelization (2.4, 2.5)
- [ ] Resolve the symbol-collision bug (1.4) before any 1000X-prefixed pairs are in scope
- [ ] Test coverage for `daemon.py::supervise` and all CLI runners (8.1)
- [ ] Frontend: fix `innerHTML`/localStorage patterns (5.10, 7.2)
- [ ] `chmod 600` generated `.env` in `bootstrap-env.sh` (5.8)

**Medium priority (address within first 1-2 quarters):**
- [ ] Investigate and resolve the Phase 1 "zero signals on real data" finding as a product issue, not just a calibration footnote (Section 8/Top Risks)
- [ ] Batch signal/event persistence (4.4)
- [ ] `_persist` performance fix in `SymbolManager` (4.1)
- [ ] Resource limits on all compose services (9.3)
- [ ] `numeric` precision/scale fix (3.4)
- [ ] CSV loader row-level error handling (2.7)
- [ ] Move regime thresholds into `Settings` (1.7)
- [ ] Frontend accessibility pass (7.3)

---

## Production Readiness Score: 35 / 100

**Justification:** The codebase demonstrates real engineering discipline — parameterized SQL throughout (no injection risk found anywhere), an honest and verifiable bug-fix history across milestones (race conditions and event-spam bugs found and fixed with regression tests, test counts independently confirmed at 164/165 passing), bounded memory under load (independently plausible from code review), and a genuinely above-average dev/prod Compose split. These are not nothing — a lot of repos at this stage have none of this.

But the score is dragged down hard by issues that are specifically about *production* readiness rather than *code quality*: the production image is the dev image (root user, dev tooling shipped); the headline database technology (TimescaleDB hypertables/compression) has never been run against the real thing; a documented core feature (M3 dynamic symbol management) isn't deployed at all; the batch-persistence layer silently drops data on transient errors with no retry and no alerting; a money/decision-relevant DB write path will throw on first use; there is no metrics/alerting infrastructure of any kind despite a schema table that implies otherwise; and the flagship analytics feature (BUY/SELL signals) produces zero output on real-world data per the team's own Phase 1 audit. None of these are "the code is buggy" problems exactly — they're "this has not yet been operated as a production system" problems, which is exactly what the score should reflect.

## Launch Recommendation: **Ready for Private Alpha**

Suitable for: a small number of trusted internal users viewing dashboards backed by synthetic or carefully-curated real data, with active human monitoring of logs (since automated alerting doesn't exist), and with the explicit understanding that BUY/SELL signals are not currently expected to fire on real 1m data. **Not suitable** for public beta or any scenario involving real capital allocation decisions until the "Blocking" checklist above is cleared — particularly the TimescaleDB validation (3.1), the production Dockerfile (9.1), the silent-data-loss paths (2.1, 2.2), and the M3 deployment decision (1.1).

---

## Top 10 Risks (ranked)

1. **[CRITICAL]** TimescaleDB hypertables/compression have never been validated against real TimescaleDB — the entire data-layer scalability story is unproven. (`alembic/versions/0001_initial_schema.py:66,76,95,105`)
2. **[CRITICAL]** Production Docker image is the dev image — root user, dev tooling, no multi-stage build. (`docker-compose.prod.yml:9`, `Dockerfile.dev`)
3. **[CRITICAL]** `BatchWriter` silently and permanently drops up to 200 candle/indicator rows on any transient sink error, with no retry, no dead-letter, no alert. (`src/orvixa/persistence/batch_writer.py:95-105`)
4. **[HIGH]** `app_env` typo silently disables CORS restrictions, API-key enforcement, and default-credential rejection simultaneously. (`src/orvixa/config.py:120,135-164`)
5. **[HIGH]** No exception handling in `SymbolManager._loop`/`AnalyticsEngine._loop` — a single transient error permanently kills the symbol-tiering or regime/health-score pipeline with no restart and minimal logging. (`src/orvixa/symbols/manager.py:134-142`, `src/orvixa/analytics/engine.py` `_loop`)
6. **[HIGH]** `MarketReportRepository.insert` will throw on first real use — `scenarios` dict not jsonb-serialized. (`src/orvixa/db/repository.py:379-397`)
7. **[HIGH]** M3 Symbol Manager (dynamic discovery/tiering/promotion) is fully implemented but not deployed in either compose file — the live universe is frozen at startup config. (`docker-compose.dev.yml`, `docker-compose.prod.yml`)
8. **[HIGH]** No metrics/alerting infrastructure of any kind, and the `telegram_alerts` table/repository is schema-only with no actual sending code — every failure mode above is invisible except via manual log review. (`src/orvixa/api/app.py`, `alembic/versions/0001_initial_schema.py:175-194`)
9. **[HIGH]** BUY/SELL signal engine produces zero output on real BTC_REAL/ETH_REAL 1-minute data per the team's own Phase 1 audit — `trend.strength`'s 60% weight in the confidence formula structurally caps real-data confidence ~12-16 points below `signal_min_confidence=60`. (`src/orvixa/analytics/trend.py`, `src/orvixa/analytics/signals.py:105-110`, `PHASE1_AUDIT_REPORT.md`)
10. **[MEDIUM-HIGH]** No dependency lockfile — unbounded `>=` ranges mean non-reproducible builds and an unauditable runtime dependency set. (`pyproject.toml:12-31`)

---

## 30-Day Production Plan

**Week 1 — Validate the foundation, stop the bleeding on data loss**
- Stand up a real `timescale/timescaledb:2.17.2-pg16` instance (resolve the Docker Hub rate-limit issue via an authenticated pull or a mirrored image) and run `alembic upgrade head` end-to-end; verify `timescaledb_information.hypertables` and `compression_settings` show the expected rows for `candles`/`indicators`. This single task de-risks (1) above and unblocks confidence in everything downstream.
- Fix `MarketReportRepository.insert`'s jsonb bug (2.3) and add a serialization-aware test for all 4 jsonb-writing repositories.
- Fix `BatchWriter._flush` to retry-once-then-dead-letter on sink failure, with a counter exposed in logs at minimum (2.1).
- Add `try/except Exception: logger.exception(...)` around the cycle bodies of `SymbolManager._loop` and `AnalyticsEngine._loop` (1.2, 1.3).
- Add `Field(gt=0)` to all period/window `Settings` fields (2.6).

**Week 2 — Production image, secrets, and config hardening**
- Author `Dockerfile.prod`: multi-stage, `pip install ".[api]"` only into a venv, non-root `USER`, copy only `src` + venv into the final stage. Update `docker-compose.prod.yml` to reference it.
- Generate a dependency lockfile (`uv lock` or equivalent); pin `requires-python = ">=3.11,<3.13"`.
- Convert `app_env` to a `Literal["development","staging","production"]` and re-verify all three security validators trigger correctly under each value, including a deliberately-misspelled value (should now fail at config load, not silently degrade).
- Fix `hmac.compare_digest` in `auth.py` (5.3); `chmod 600` in `bootstrap-env.sh` (5.8).
- Decide and document the M3 deployment question (1.1) — either add `orvixa-symbols` to both compose files (it's fully implemented, this is config-only) or formally descope it for v1 with a note in the architecture docs.

**Week 3 — Observability and alerting baseline**
- Add a `/metrics` endpoint (Prometheus format) exposing: `batch_writer_dropped_total`, `batch_writer_flush_total`, `feed_connect_total`, `feed_reconnect_total`, `feed_gapfill_total`/`gapfill_failures_total`, `regime_refresh_count` + `regime_refresh_age_seconds`, `symbol_manager_refresh_duration_seconds`, `symbol_manager_tier_changes_total`.
- Extend `/health` to perform a real `SELECT 1` against the DB pool and report feed-connection status.
- Stand up a minimal Prometheus + Grafana (or equivalent) stack via a new compose service, with 3-5 dashboards covering the metrics above.
- Implement at least the Telegram-send portion of M7 for a small set of critical alerts (batch-writer data loss, regime-refresh staleness, feed disconnected >N minutes) — even a minimal implementation closes the "zero alerting" gap (10.2).
- Fix `print()` → logger in `signal_validation.py` (10.4).

**Week 4 — API contract, testing gaps, and the signal-confidence product question**
- Add Pydantic response models for all 5 API endpoints, prefix routes with `/v1`, add pagination to `/symbols` and `/signals/{symbol}` (6.1-6.3).
- Add rate limiting via `slowapi` + the existing Redis instance (6.5).
- Write `daemon.py::supervise` unit tests and at least smoke tests for each CLI runner's startup/shutdown path (8.1).
- Write the symbol-collision regression test referenced (but never committed) in `M3_VALIDATION_REPORT_FINAL.md` (8.5, 1.4).
- Fix the frontend `innerHTML`/localStorage patterns (5.10, 7.2) and add basic `aria-live` regions (7.3).
- **Product decision required**: convene with stakeholders on the Phase 1 "zero signals on real data" finding (9 in Top 10 Risks). Either (a) recalibrate `signal_min_confidence`/the confidence-formula weights against real data and re-validate with a fresh Phase 1-style audit, or (b) explicitly reposition the BUY/SELL signal feature as "tuned for higher-volatility timeframes/symbols" with documentation reflecting that 1m BTC/ETH is out of scope for v1. Either outcome is acceptable, but shipping with this unresolved means a core advertised feature silently does nothing for the most obvious use case.

By end of Week 4: TimescaleDB validated, no silent data loss, production image is actually a production image, basic metrics/alerting exist, API has a real contract, and the signal-confidence gap has a documented resolution. This would move the system from **Private Alpha** to a defensible **Limited Beta** candidate — full **Ready for Production** would still require the Medium-priority checklist items (retention policies, continuous aggregates, M3 performance fix, resource limits, backup automation with tested restores) plus a longer real-data observation window.
