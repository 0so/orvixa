# Orvixa — Production Hardening Plan

Derived from `PRODUCTION_READINESS_AUDIT.md`. Scope: **hardening only** — no redesign, no new features, no UI work. Goal: shortest safe path from **Private Alpha → Limited Beta → Production**.

Frontend findings (5.10, 7.1-7.4) are explicitly **deferred** per scope (UI excluded) but are flagged in P2 as a follow-up security pass once UI work resumes — `innerHTML`/localStorage is a real XSS-adjacent issue and should not be forgotten.

---

## P0 — Must fix immediately (data loss, silent total failure, trivial security holes)

These are cheap (S effort, mostly <1 day each) and each one currently means "the system can break completely and nobody will know, or a one-line config typo disables security."

### P0-1: BatchWriter silently drops data on sink failure
- **Files:** `src/orvixa/persistence/batch_writer.py:95-105` (`_flush`), `:59-68` (`stop`)
- **Root cause:** `batch, self._buffer = self._buffer, []` happens before `await self._sink(batch)`; on exception the batch is gone, only `error_count` increments.
- **Implementation steps:**
  1. On `_sink(batch)` exception, re-prepend `batch` to `self._buffer` (front, preserving order) instead of discarding.
  2. Add a `_consecutive_failures` counter and a `max_retry_batches` cap (e.g. 5x `max_size`) — if exceeded, log CRITICAL and drop oldest batch (bounded loss, not unbounded buffer growth).
  3. Apply the same retry-on-failure logic to the final flush in `stop()`.
  4. Update `test_sink_error_is_isolated_and_counted` to assert the batch is retried on the next `_flush` call, not lost.
- **Effort:** M (1-2 days)
- **Production impact:** Eliminates silent permanent gaps in `candles`/`indicators` from transient DB blips — the single biggest data-integrity risk in the system.
- **Risk reduction:** Top Risk #3 (CRITICAL) → resolved.

### P0-2: `_loop` tasks die silently on any exception
- **Files:** `src/orvixa/symbols/manager.py:134-142` (`SymbolManager._loop`), `src/orvixa/analytics/engine.py` `_loop` (~126-134)
- **Root cause:** Only `asyncio.CancelledError` is caught/re-raised; any other exception kills the `asyncio.Task` forever with `_running` still `True`.
- **Implementation steps:**
  1. In both `_loop` methods, wrap the per-cycle body (`await self.refresh_universe()` / `await self.refresh_regime()`) in `try/except Exception: logger.exception("...cycle failed")`, matching `BinanceFeed._run`'s existing pattern.
  2. Continue the loop (`await asyncio.sleep(interval)` then retry) after logging.
  3. Increment a `cycle_error_count` attribute for later exposure via `/metrics` (P1-7).
- **Effort:** S (0.5 day)
- **Production impact:** Prevents permanent silent death of the symbol-tiering and regime/health-score pipelines from a single transient error (e.g. one DB hiccup).
- **Risk reduction:** Top Risk #5 (HIGH) → resolved.

### P0-3: `MarketReportRepository.insert` jsonb bug
- **Files:** `src/orvixa/db/repository.py:379-397`
- **Root cause:** `row.scenarios` (a `dict`) passed directly as a bind parameter to a `jsonb` column with no `json.dumps()` / `::jsonb` cast — unlike `SignalRepository.insert`, `MarketEventRepository.insert`, `MarketMemoryRepository.insert_snapshot` which all do this correctly.
- **Implementation steps:**
  1. Change the bind to `json.dumps(row.scenarios)` and the placeholder to `$3::jsonb` (match the established pattern from the three sibling repos).
  2. Extend `tests/test_repository.py`'s `FakePool` (or `test_db_integration.py`) to actually serialize/round-trip jsonb for **all 4** jsonb-writing repositories (Signal, MarketEvent, MarketMemory, MarketReport) — closes audit finding 8.2.
- **Effort:** S (0.5 day)
- **Production impact:** Prevents a guaranteed crash the moment M6 (`market_reports`) is exercised.
- **Risk reduction:** Top Risk #6 (HIGH) → resolved.

### P0-4: `app_env` is an unconstrained string
- **Files:** `src/orvixa/config.py:120,135-164`
- **Root cause:** `app_env: str = "production"`; three security validators (`_validate_cors_origins`, `_validate_api_key`, `_validate_no_default_credentials`) gate on `.lower() == "production"`. Any other string (typo, trailing whitespace, `"prod"`) silently falls into dev-mode behavior with CORS wildcard, no API key required, default creds allowed.
- **Implementation steps:**
  1. Change `app_env` to `Literal["development", "staging", "production"]` (pydantic rejects anything else at startup with a clear error).
  2. Invert default-safety direction where practical: treat anything that isn't exactly `"development"` as requiring production-grade settings (defense in depth even if the Literal is bypassed via env var injection from outside pydantic's validation, which it can't be — but document the intent).
  3. Add a config test: `Settings(app_env="Production")` (capital P) should now fail validation instead of silently degrading.
- **Effort:** S (0.5-1 day)
- **Production impact:** Closes the single highest-leverage misconfiguration — one bad `.env` line can no longer silently disable CORS + auth + credential checks simultaneously.
- **Risk reduction:** Top Risk #4 (HIGH) → resolved.

### P0-5: Config period/window settings unvalidated
- **Files:** `src/orvixa/config.py` (all `*_period`, `*_window` fields), `src/orvixa/analytics/indicators.py`, `src/orvixa/analytics/events.py`
- **Root cause:** No `Field(gt=0)` (or `ge=2`) on `rsi_period`, `atr_period`, `ema_fast_period`, `ema_slow_period`, `realized_vol_window`, `relative_volume_window`, `breakout_window`, `pump_dump_window`, `vol_spike_window`. A value of `0`/negative causes `ZeroDivisionError`/`ValueError("maxlen must be non-negative")` inside `handle_candle`, caught only by `feeds/base.py`'s broad per-callback handler — recurring log spam, analytics permanently dead, no startup signal.
- **Implementation steps:**
  1. Add `Field(gt=0)` to every period/window setting in `config.py` (use `ge=2` where a 1-element window is meaningless, e.g. EMA periods).
  2. Add a config test instantiating `Settings` with `RSI_PERIOD=0` and asserting it raises at construction time.
- **Effort:** S (0.5 day)
- **Production impact:** Converts a runtime "analytics silently dead forever, logs every candle" failure into a startup-time config error.
- **Risk reduction:** Removes a class of "works in dev, breaks via one env var in prod" failures.

### P0-6: API key comparison is not constant-time
- **Files:** `src/orvixa/api/auth.py:27-29`
- **Root cause:** `if x_api_key != expected:` — timing side-channel; also `if not expected: return` (empty key disables auth entirely, relying on P0-4's fragile gate).
- **Implementation steps:**
  1. `if not hmac.compare_digest(x_api_key or "", expected): raise HTTPException(401)`.
  2. Once P0-4 lands, also consider making "empty API key" a hard config-validation error in production rather than an auth bypass (covered by existing `_validate_api_key`, just re-verify it still triggers with the new `Literal`).
- **Effort:** S (<0.5 day)
- **Production impact:** Removes a textbook timing side-channel on the only auth mechanism.
- **Risk reduction:** Low-probability but zero-cost fix; do it now while touching `auth.py`/`config.py` anyway.

### P0-7: `bootstrap-env.sh` doesn't restrict `.env` permissions
- **Files:** `scripts/bootstrap-env.sh`
- **Root cause:** Generated `.env` (containing `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `API_KEY`) is written with default umask (typically world-readable).
- **Implementation steps:** Add `chmod 600 "$OUT"` immediately after the file is written.
- **Effort:** S (<0.25 day)
- **Production impact:** Prevents secrets in `.env` from being world-readable on shared hosts.
- **Risk reduction:** Trivial, do immediately.

### P0-8: `make down` deletes volumes (database wipe)
- **Files:** `Makefile`
- **Root cause:** `down` target runs `docker compose down -v`, destroying the Postgres/Redis volumes — a single muscle-memory `make down` during an incident wipes the database.
- **Implementation steps:**
  1. Split into `down` (no `-v`, safe) and `down-clean`/`nuke` (explicit, with `-v`, for dev resets only).
  2. Document the distinction in `README.md`/runbook.
- **Effort:** S (<0.25 day)
- **Production impact:** Removes an operator-error landmine that's trivial to hit under incident pressure.
- **Risk reduction:** High leverage for near-zero cost.

**P0 total: ~6 effort-days, fully parallelizable across 2-3 engineers in ~2-3 calendar days.**

---

## P1 — Must fix before Limited Beta

These are the items that gate "can we let real-but-trusted users see this running on real data, with someone watching."

### P1-1: TimescaleDB hypertables/compression never validated against real TimescaleDB
- **Files:** `alembic/versions/0001_initial_schema.py:66,76,95,105`, CI config (none currently does this)
- **Root cause:** M2's "live-DB smoke test" ran against plain Postgres 16 (no TimescaleDB extension) due to a Docker Hub rate-limit/packagecloud 403 — `create_hypertable`/`add_compression_policy` were validated as "syntactically valid SQL" only, never executed.
- **Implementation steps:**
  1. Resolve the image-pull issue: authenticate to Docker Hub (or use an internal mirror/cache) to pull `timescale/timescaledb:2.17.2-pg16`.
  2. In CI (and locally), run `alembic upgrade head` against that image end-to-end.
  3. Assert `SELECT * FROM timescaledb_information.hypertables` returns `candles` and `indicators`, and `timescaledb_information.compression_settings` shows the expected 7-day compression policy for both.
  4. If `create_hypertable`/`add_compression_policy` fail or behave unexpectedly, fix the migration **before** proceeding to P1-3 (retention policies depend on this schema being correct).
  5. Wire this check into CI as a required job (not just a one-time manual run) so future migrations can't silently break hypertable status.
- **Effort:** M (1-2 days; mostly waiting on infra access, the assertions themselves are quick)
- **Production impact:** De-risks the entire data-layer scalability story. If this fails, `candles`/`indicators` are plain unpartitioned tables that degrade badly at scale.
- **Risk reduction:** Top Risk #1 (CRITICAL) → resolved or surfaces a real migration bug to fix now rather than after first production deploy.

### P1-2: Production image is the dev image
- **Files:** `docker-compose.prod.yml:9`, `Dockerfile.dev`, new `Dockerfile.prod`
- **Root cause:** `docker-compose.prod.yml` builds from `Dockerfile.dev`, which `pip install -e ".[dev,api]"` — ships pytest/ruff/mypy/sqlalchemy/alembic dev deps into prod, runs as root, no multi-stage build, no `HEALTHCHECK` in the image.
- **Implementation steps:**
  1. Create `Dockerfile.prod`: multi-stage — build stage installs `.[api]` only into a venv; final stage is a slim Python base image, copies venv + `src`, sets a non-root `USER orvixa`.
  2. Add an image-level `HEALTHCHECK` (e.g. hitting `/health` for the API image, or a lightweight `python -c` liveness check for the daemon images).
  3. Update `docker-compose.prod.yml` to reference `Dockerfile.prod`.
  4. Re-run the full test suite + a smoke `docker compose -f docker-compose.prod.yml up` against the new image to confirm nothing in `[dev]`-only deps (sqlalchemy, alembic — wait, alembic is needed for migrations: verify which deps `[api]` actually needs vs `[dev]` and adjust `pyproject.toml`'s optional-dependency groups if `alembic`/`asyncpg` are misclassified).
- **Effort:** M (2-3 days, including dependency-group audit in `pyproject.toml`)
- **Depends on:** P1-4 (lockfile) ideally lands first so the new Dockerfile builds from pinned versions.
- **Production impact:** Removes root-user + dev-tooling attack surface from every production container; establishes real dev/prod image separation.
- **Risk reduction:** Top Risk #2 (CRITICAL) → resolved.

### P1-3: M3 Symbol Manager not deployed
- **Files:** `docker-compose.dev.yml`, `docker-compose.prod.yml`, `src/orvixa/runners/symbols.py` (already implemented)
- **Root cause:** No `orvixa-symbols` service exists in either compose file — only `migrate`, `ingest`, `analytics`, `api`, `frontend`. The live universe is frozen at whatever `seed_symbols()` wrote at startup.
- **Implementation steps:**
  1. **Decision required first** (product/ops call, not engineering): deploy M3 now, or formally descope dynamic discovery for v1 and document it.
  2. If deploying: add `orvixa-symbols` service to both compose files mirroring the `<<: *app` anchor pattern used by `ingest`/`analytics`, running `python -m orvixa.runners.symbols` under `daemon.py` supervision.
  3. **Must land after P1-9 (symbol-collision fix)** — deploying M3 without fixing the `1000PEPE`/`PEPE`-style collision means real-world collisions become live, silent data-loss events instead of theoretical ones.
  4. Also must land after P0-2 (loop exception handling) — M3's `_loop` is one of the two loops fixed there.
  5. Smoke-test in dev compose for at least 24h (covers one `symbol_refresh_interval_seconds` cycle many times over) before enabling in prod compose.
- **Effort:** S (config-only, <1 day) once P1-9 and P0-2 are done; the decision itself may take longer than the implementation.
- **Production impact:** Either the system's "live universe" claims become true, or the team stops implying a capability that doesn't run.
- **Risk reduction:** Top Risk #7 (HIGH) → resolved (one way or the other).

### P1-4: No dependency lockfile
- **Files:** `pyproject.toml:12-31`, new lockfile
- **Root cause:** All deps use unbounded `>=` ranges; no `requirements*.txt`/`poetry.lock`/`uv.lock`/`Pipfile.lock`.
- **Implementation steps:**
  1. Adopt `uv` (fastest, simplest fit for `pyproject.toml`-based projects): `uv lock`, commit `uv.lock`.
  2. Pin `requires-python = ">=3.11,<3.13"`.
  3. Update `Dockerfile.dev` and the new `Dockerfile.prod` (P1-2) to install from the lockfile (`uv sync --frozen`).
  4. Add a CI job that fails if `uv.lock` is out of sync with `pyproject.toml`.
- **Effort:** S-M (1 day)
- **Depends on:** None — should land **before** P1-2 so the new prod Dockerfile is built against pinned versions from day one.
- **Production impact:** Reproducible builds; enables CVE/SBOM analysis of what's actually deployed.
- **Risk reduction:** Top Risk #10 (MEDIUM-HIGH) → resolved.

### P1-5: `refresh_regime` silently stops persisting during feed outages
- **Files:** `src/orvixa/analytics/engine.py` `refresh_regime` (~line 224)
- **Root cause:** `if self._latest_breadth is None or not self._latest_trend: return None` — during a sustained reconnect-backoff window, `market_memory` simply stops being written with zero alerting.
- **Implementation steps:**
  1. Track `_consecutive_regime_noops`; log a WARNING on the first no-op and every Nth consecutive no-op thereafter (e.g. every 10).
  2. Add `regime_refresh_count` and `last_regime_refresh_ts` as attributes exposed via `/metrics` (P1-7).
- **Effort:** S-M (1 day)
- **Production impact:** Converts a silent, invisible pipeline stall into a logged + (post-P1-7) alertable condition.
- **Risk reduction:** Closes a major contributor to "system degrades with zero operator-visible signal."

### P1-6: No WebSocket liveness watchdog
- **Files:** `src/orvixa/feeds/binance.py:190-212` (`_connect_once`), `:266-269` (`_default_connector`)
- **Root cause:** Relies entirely on `websockets`' built-in `ping_interval=20, ping_timeout=20`; no independent "no message received in N seconds → force reconnect" check.
- **Implementation steps:**
  1. Track `_last_message_at` (update on every received message, including pings handled internally by the library if visible, otherwise on every `async for raw in ws` iteration).
  2. Add a watchdog coroutine (or check inside `_run`'s loop) that force-closes and reconnects if `time.monotonic() - _last_message_at > watchdog_timeout_seconds` (new config field, e.g. default 60s).
  3. Increment `watchdog_reconnect_count` for `/metrics`.
- **Effort:** M (1-2 days)
- **Production impact:** Catches "connected but silent" half-open connections that the library's own keepalive might miss.
- **Risk reduction:** Reduces window of undetected candle gaps from "until the connection eventually errors" to "≤60s."

### P1-7: No `/metrics` endpoint, `/health` doesn't check DB
- **Files:** `src/orvixa/api/app.py`, new `src/orvixa/api/metrics.py` (or similar)
- **Root cause:** Zero Prometheus/OTel integration anywhere; `/health` returns static `{"status": "ok"}` with no DB connectivity check, so a DB outage looks "healthy" to load balancers/compose healthchecks.
- **Implementation steps:**
  1. Add `prometheus-client` (or reuse existing deps if any expose counters) and a `/metrics` route.
  2. Expose at minimum: `batch_writer_dropped_total`, `batch_writer_flush_total`, `feed_connect_total`, `feed_reconnect_total`, `feed_gapfill_total`/`gapfill_failures_total` (P1-8 depends on this existing), `regime_refresh_count`, `regime_refresh_age_seconds` (P1-5), `symbol_manager_refresh_duration_seconds`, `symbol_manager_tier_changes_total`, `cycle_error_count` (P0-2).
  3. Extend `/health` to run a cheap `SELECT 1` against the read pool with a short timeout; return 503 if it fails or times out.
  4. Update `docker-compose.prod.yml`'s healthcheck to treat a 503 from `/health` as unhealthy (likely already does via HTTP status — verify).
- **Effort:** M (2-3 days)
- **Depends on:** P0-1, P0-2, P1-5, P1-6, P1-9 — each adds a counter this endpoint exposes. Land this *after* those (or stub the endpoint early and backfill counters incrementally).
- **Production impact:** First real operational visibility into the system; required before any alerting (P1-10) is meaningful.
- **Risk reduction:** Top Risk #8 (HIGH, partial) → resolved for the "no metrics" half.

### P1-8: Gap-fill failure on reconnect is silent and unretried
- **Files:** `src/orvixa/feeds/binance.py:257-259` (`_gap_fill`), `:271-285` (`_default_backfiller`)
- **Root cause:** Broad `except Exception` + log only, `gapfill_count` not incremented on failure; backfiller issues sequential per-symbol REST calls.
- **Implementation steps:**
  1. Retry `_gap_fill` with backoff (2-3 attempts) before giving up.
  2. Track `gapfill_failures_total` (feeds P1-7).
  3. Parallelize `_default_backfiller`'s per-symbol REST calls via `asyncio.gather` with a concurrency cap (e.g. 5) to respect Binance rate limits.
- **Effort:** M (1-2 days)
- **Production impact:** Restores the "no minute is ever lost" guarantee under realistic reconnect-during-outage conditions; faster gap-fill on reconnect.
- **Risk reduction:** Closes a real (not hypothetical) candle-gap source.

### P1-9: Symbol-collision bug (`1000PEPE`/`PEPE`)
- **Files:** `src/orvixa/feeds/normalize.py` (suffix-stripping, lines ~25-31), `src/orvixa/symbols/manager.py:251-272` (`_sync_listings`, keys `_states` by `base`)
- **Root cause:** `PEPEUSDT` and `1000PEPEUSDT` both normalize to base `"PEPE"`; whichever is processed second in `_sync_listings` silently overwrites the first's `_SymbolState` — no error, no log. Documented in `M3_VALIDATION_REPORT_FINAL.md` §3.2 as "reported, not fixed."
- **Implementation steps:**
  1. Key `_states` (and any downstream lookups) by `pair` (the actual Binance symbol, e.g. `"1000PEPEUSDT"`) instead of normalized `base`.
  2. Check schema implications: if `symbols.base` has a uniqueness constraint, either relax it or add `pair` as the true unique key (likely already is — verify against `0001_initial_schema.py`).
  3. Add the regression test referenced-but-never-committed in `M3_VALIDATION_REPORT_FINAL.md` (two-pair fixture: `PEPEUSDT` + `1000PEPEUSDT` both active, assert both retain independent `_SymbolState` entries).
- **Effort:** M (2-3 days — touches `_states` keying throughout `manager.py` plus schema verification)
- **Must complete before:** P1-3 (M3 deployment) if M3 is being deployed.
- **Production impact:** Prevents silent disappearance of one pair's market data/tier/feed-subscription whenever a "1000X"-prefixed meme coin and its base pair are both listed — a recurring, realistic Binance pattern.
- **Risk reduction:** Closes a known, documented, unfixed data-correctness bug.

### P1-10: `telegram_alerts` is schema-only — no alerting exists
- **Files:** `alembic/versions/0001_initial_schema.py:175-194`, `src/orvixa/db/repository.py`/`db/models.py` (`TelegramAlertRepository`/`Row` exist), no sending code anywhere
- **Root cause:** Table + repo + dedupe-key unique index all exist; no bot token, webhook, or send implementation in `config.py`/`.env.example`/any runner.
- **Implementation steps (minimal scope — not full M7):**
  1. Add `telegram_bot_token`/`telegram_chat_id` to `Settings` (optional; alerting disabled if unset).
  2. Implement a small `alerts.py` module: given a `TelegramAlertRow`, send via Telegram Bot API (`httpx` POST), respecting the existing `dedupe_key` unique index (insert-or-skip via `ON CONFLICT DO NOTHING`, only send if the insert actually wrote a row).
  3. Wire exactly 3 critical alert sources into it: (a) P0-1's batch-writer permanent-drop event, (b) P1-5's regime-refresh staleness crossing a threshold, (c) feed disconnected >N minutes (from P1-6's watchdog).
  4. If descoped instead: update `DATASET.md`/architecture docs to explicitly state `telegram_alerts` is schema-only/dormant, so it isn't mistaken for a working safety net.
- **Effort:** L (3-5 days for minimal implementation) / S (0.5 day for documentation-only descope)
- **Depends on:** P0-1, P1-5, P1-6 (need the events to alert on to exist first), P1-7 (alerting and metrics share the same underlying counters).
- **Production impact:** Without this, every failure mode fixed above is still only visible via manual log review. This is what makes Limited Beta "safe to leave unattended overnight."
- **Risk reduction:** Top Risk #8 (HIGH, remaining half) → resolved.

### P1-11: Backup/restore strategy
- **Files:** new — `scripts/backup-db.sh`, `Makefile`, possibly a new compose sidecar service
- **Root cause:** No `pg_dump` cron, no volume-snapshot automation, no documented restore procedure anywhere.
- **Implementation steps:**
  1. Add a `pg_dump`-based backup script, runnable via cron (host-level) or a sidecar container in `docker-compose.prod.yml`.
  2. Document (and test once) a full restore procedure into a fresh `timescale/timescaledb` container.
  3. Add `make backup-db` / `make restore-db` targets (also addresses audit 9.5's "no prod Makefile targets").
  4. Decide and document offsite storage (S3/equivalent) — at minimum, document the manual step if automation is out of scope for now.
- **Effort:** M (2-3 days, including one real restore-drill)
- **Depends on:** P1-1 (need real TimescaleDB running to validate backup/restore against hypertables — `pg_dump` of hypertables has known caveats worth confirming).
- **Production impact:** Without this, any data corruption/loss event (including ones from bugs not yet found) is unrecoverable.
- **Risk reduction:** Closes a complete operational gap; required before any real user data accumulates.

### P1-12: `daemon.py::supervise` and CLI runners have zero test coverage
- **Files:** `src/orvixa/runners/{ingest,analytics,symbols,backfill,daemon,feedcheck}.py`, new `tests/test_daemon.py`
- **Root cause:** No tests exist for the restart-on-crash/restart-on-exit/signal-handling supervisor — the entire "Phase 2 keep-services-alive" mechanism is unverified.
- **Implementation steps:**
  1. Unit test `supervise()` with a fake `run()` coroutine that raises N times then succeeds — assert restart count, backoff/interval behavior.
  2. Test SIGINT/SIGTERM handling triggers graceful shutdown (no restart after intentional stop).
  3. Add minimal startup/shutdown smoke tests for each CLI runner (can mostly mock the underlying engine/manager — goal is "does the entrypoint wire up and tear down cleanly," not full integration).
- **Effort:** M (2-3 days)
- **Production impact:** A regression in the crash-restart logic (the actual production resilience mechanism) currently can't be caught by `make test` — it would surface as "the ingest daemon stopped and didn't come back" in prod.
- **Risk reduction:** Closes the largest test-coverage gap relative to its operational importance.

### P1-13: Signal-confidence "zero output on real data" — product decision
- **Files:** `src/orvixa/analytics/trend.py`, `src/orvixa/analytics/signals.py:105-110`, `PHASE1_AUDIT_REPORT.md`
- **Root cause:** Per the team's own Phase 1 audit, `trend.strength`'s 60% weight in the confidence formula structurally caps real-data confidence ~12-16 points below `signal_min_confidence=60` — BUY/SELL signals never fire on real BTC/ETH 1m data.
- **Implementation steps (this is a decision + validation task, not a redesign):**
  1. Re-run the Phase 1-style validation against real data with the current code to confirm the gap still exists as measured.
  2. Decision (product/stakeholder): (a) recalibrate `signal_min_confidence` and/or the confidence-formula weights against real data and re-validate, OR (b) explicitly document that 1m BTC/ETH is out of scope for v1 signal generation and reposition the feature accordingly.
  3. Whichever path: update `PHASE1_AUDIT_REPORT.md`'s status and add a regression test asserting the chosen behavior (either "signals fire under X real-data condition" or "documented non-goal, test asserts current threshold").
- **Effort:** M (2-3 days analysis + whichever fix path is chosen; recalibration itself could be S if it's just threshold tuning, M-L if formula weights need rework — but "rework formula weights" starts to brush against "redesign," so default to threshold/config tuning only within this plan's scope)
- **Production impact:** Shipping with this unresolved means a headline feature silently does nothing on the most obvious real-world input — a credibility/trust issue the moment a beta user looks at real BTC/ETH signals and sees nothing.
- **Risk reduction:** Top Risk #9 (HIGH) → resolved or formally scoped out with documentation.

**P1 total: ~25-32 effort-days. With 2-3 engineers working in parallel respecting the dependency graph below, ~10-14 calendar days.**

---

## P2 — Must fix before full Production

These harden scalability, API contract stability, and close remaining test/observability gaps. Not blocking for a *limited, supervised* beta, but required before opening up to broader/public traffic or treating the system as unattended.

| # | Item | Files | Root cause (brief) | Effort | Impact |
|---|------|-------|----|--------|--------|
| P2-1 | Retention policy + continuous aggregates | `alembic/` new migration | No `add_retention_policy` for any table; `signals`/`market_events`/`market_memory`/`market_reports`/`telegram_alerts` are plain unbounded Postgres tables; no rollup aggregates for dashboard queries | L (3-5d) | Prevents unbounded table growth and raw-row-scan query costs at scale |
| P2-2 | API versioning + Pydantic response models | `src/orvixa/api/app.py` | All routes unprefixed (`/symbols` not `/v1/symbols`); responses are raw `dict(asyncpg.Record)` — OpenAPI shows `{}`, `/symbols` leaks internal columns (`tags`,`metrics`,`last_synced`,`rank`) | M (3-4d) | Real API contract, no accidental internal-field exposure, migration path for breaking changes |
| P2-3 | Pagination on `/symbols` and `/signals/{symbol}` | `src/orvixa/api/app.py`, `db/repository.py` | `/symbols` has no LIMIT; `/signals` capped at 50 with no offset/cursor | S-M (1-2d) | Prevents unbounded response sizes as symbol universe grows (esp. if M3/P1-3 deployed) |
| P2-4 | Rate limiting | `src/orvixa/api/app.py` | Single shared API key, no per-key/IP limit | S-M (1-2d, `slowapi` + existing Redis) | Bounds blast radius of a leaked key or misbehaving client |
| P2-5 | Global exception handlers + correlation IDs | `src/orvixa/api/app.py` | No `@app.exception_handler`; unhandled exceptions return generic 500 with no request correlation | S-M (1-2d) | Debuggable production incidents |
| P2-6 | `SymbolManager._persist` batching | `src/orvixa/symbols/manager.py:211-212,353-372` | Sequential per-symbol upsert+ranking (2 round trips × up to 600 symbols = 15-35s/cycle); frozen symbols not excluded | M (2-3d) | Keeps refresh cycle fast as universe grows (relevant once P1-3/M3 deployed) |
| P2-7 | Batch signal/event persistence | `src/orvixa/analytics/engine.py` ~172,186 | Individual `INSERT...RETURNING` per signal/event, no `BatchWriter` (unlike indicators) | M (2d) | Avoids sequential DB storms during multi-symbol event bursts (flash-crash scenario) |
| P2-8 | Resource limits on all compose services | `docker-compose.prod.yml` | No `mem_limit`/`cpus`/`deploy.resources.limits` anywhere | S (1d) | Caps blast radius of any runaway process (e.g. unbounded `_states` growth) |
| P2-9 | Remaining test-coverage gaps | `tests/` | No tests for real `websockets`/`httpx` connector paths (8.3), `_MAX_STREAMS_PER_SOCKET` warning path (8.4), `AnalyticsEngine.start/stop/_loop` (8.6), BatchWriter retry behavior under P0-1's new logic (8.7), CSV loader edge cases (2.7/8.8) | L (4-5d total) | Closes the gap between "164 tests pass" and "the failure paths we just hardened are actually verified" |
| P2-10 | Migration downgrade guard | `alembic/versions/0001_initial_schema.py` | `downgrade()` unconditionally `DROP TABLE`s all 8 tables, no env check | S (0.5d) | Prevents `alembic downgrade base` from wiping prod data |
| P2-11 | Move regime thresholds to `Settings` | `src/orvixa/analytics/regime.py:20-23` | `_RISK_ON_AD_RATIO` etc. hardcoded vs. other thresholds being config | S (0.5d) | Enables tuning without redeploy — useful input for P1-13's recalibration |
| P2-12 | Consolidate dual symbol-seeding paths | `src/orvixa/persistence/registry.py:49-51`, `src/orvixa/symbols/manager.py` | Two divergent code paths can seed `symbols` table depending on which runner starts first | S (1d) | Removes latent fragility, mostly relevant once P1-3/M3 is deployed |
| P2-13 | `_PROACTIVE_RECONNECT_SECONDS` dead code | `src/orvixa/feeds/binance.py:48` | Constant defined, never used; docstring overstates resilience | S (0.5d, remove+fix docs) | Documentation/code consistency; low risk either way |
| P2-14 | Log shipping to centralized store | infra (compose + external) | JSON logs exist but go nowhere; P0-1/P1-5/P1-10's log lines are only as good as someone reading them | M (2-3d, infra-heavy) | Makes the structured logging investment actually useful operationally |
| P2-15 | `print()` → logger in `signal_validation.py` | `src/orvixa/backtest/signal_validation.py` ~318,327-330 | Dataset-provenance/SYNTHETIC-data warnings go to stdout, not structured logs | S (0.25d) | Ensures "synthetic dataset used" warnings aren't missed in automated runs |
| P2-16 | App-factory import-time side effects | `src/orvixa/api/app.py:122` | `app = create_app()` at module scope means importing the module constructs `Settings()`, which raises in misconfigured prod envs | S (1d) | Cleaner failure mode for misconfiguration; currently masked by `conftest.py` env-setting |
| P2-17 | (Deferred-UI flag) `innerHTML`/localStorage XSS hardening | `frontend/index.html` | Out of scope for this plan (no UI work), but tracked here so it isn't lost — should be the first item when UI work resumes | — | Security follow-up, not part of backend hardening timeline |

**P2 total: ~28-35 effort-days, ~10-15 calendar days with 2-3 engineers.**

---

## P3 — Nice to have (post-Production, opportunistic)

| # | Item | Files | Effort |
|---|------|-------|--------|
| P3-1 | `_rank_universe` double-sort elimination | `src/orvixa/symbols/manager.py:222-240` | S |
| P3-2 | Reuse `httpx.AsyncClient` across calls in `BinanceMarketClient` | `src/orvixa/symbols/client.py:60,86` | S |
| P3-3 | Parallelize remaining sequential backfill calls (beyond P1-8's gap-fill scope) | `src/orvixa/feeds/binance.py` | S |
| P3-4 | CSV loader row-level error context + OHLC sanity checks | `src/orvixa/backfill/csv_loader.py` | S-M |
| P3-5 | `update_ranking` result-status assertion | `src/orvixa/db/repository.py:83-90` | S |
| P3-6 | `numeric` → `numeric(18,8)`/`double precision` precision fix | `alembic/` new migration | M (data migration care) |
| P3-7 | Index on `symbols.rank` | `alembic/` new migration | S |
| P3-8 | Alembic `env.py` DSN-rewrite robustness (`postgres://` scheme) | `alembic/env.py:30-34` | S |
| P3-9 | Docker layer-cache reordering in `Dockerfile.prod`/`Dockerfile.dev` | `Dockerfile.*` | S |
| P3-10 | `_sync_feed` partial-failure handling beyond P0-2's loop-level catch | `src/orvixa/symbols/manager.py:375-393` | S |

**P3 total: ~8-10 effort-days — schedule opportunistically, not on the critical path.**

---

## Dependency Graph (optimal execution order)

```
PHASE 0 (parallel, ~2-3 days, no dependencies between them)
├── P0-1  BatchWriter retry/dead-letter
├── P0-2  _loop exception handling          ──┐ (feeds P1-3, P1-7, P1-10)
├── P0-3  jsonb fix + repo test              │
├── P0-4  app_env Literal                    ├─→ (feeds P1-2's re-validation)
├── P0-5  config Field(gt=0)                 │
├── P0-6  hmac.compare_digest                │
├── P0-7  bootstrap-env.sh chmod             │
└── P0-8  Makefile down/down-clean split    ─┘

PHASE 1A (can start immediately, parallel to Phase 0)
├── P1-4  Dependency lockfile  ───────────────────┐
├── P1-1  TimescaleDB validation  ────────────────┼─→ P1-11 (backup/restore needs real TimescaleDB)
└── P1-9  Symbol-collision fix  ───────────────────┐
                                                    │
PHASE 1B (depends on Phase 0 + P1-4)               │
├── P1-2  Production Dockerfile  (needs P1-4)      │
├── P1-5  regime-refresh staleness logging          │
├── P1-6  WS liveness watchdog                      │
├── P1-8  gap-fill retry/parallelize                │
└── P1-12 daemon/runner test coverage               │
                                                    │
PHASE 1C (depends on P0-2 + P1-9, and P1-5/P1-6/P1-8 for counters)
├── P1-3  M3 deployment decision+config  (needs P0-2, P1-9)
├── P1-7  /metrics + /health DB check    (needs P0-1,P0-2,P1-5,P1-6,P1-9 for counters — can stub early, backfill incrementally)
└── P1-13 Signal-confidence decision      (independent — can run in parallel with anything)

PHASE 1D (depends on Phase 1C)
├── P1-10 Telegram alerting (minimal)     (needs P1-5, P1-6, P1-7)
└── P1-11 Backup/restore                  (needs P1-1)

═══════════════ LIMITED BETA GATE ═══════════════

PHASE 2 (mostly parallel, some ordering preferences)
├── P2-2  API versioning + response models  ──→ P2-3 pagination (same files, do together)
├── P2-4  Rate limiting
├── P2-5  Exception handlers + correlation IDs
├── P2-1  Retention policy + continuous aggregates  (do after P1-1 confirms hypertable status)
├── P2-6  SymbolManager._persist batching   (higher value if P1-3 deployed M3)
├── P2-7  Batch signal/event persistence
├── P2-8  Resource limits
├── P2-9  Remaining test coverage
├── P2-10 Downgrade guard
├── P2-11 Regime thresholds → config        (do before/with P1-13 if recalibration path chosen)
├── P2-12 Consolidate seeding paths
├── P2-13 Dead code cleanup
├── P2-14 Log shipping
├── P2-15 print()→logger
└── P2-16 App-factory pattern

═══════════════ PRODUCTION GATE ═══════════════

P3 — opportunistic, no gate
```

---

## Estimates

### Days to Limited Beta
- **P0: 2-3 calendar days** (parallel, 2-3 engineers)
- **P1: 10-14 calendar days** (parallel where the graph allows, respecting P0-2/P1-9 → P1-3/P1-7/P1-10 chain and P1-1 → P1-11)
- **Total: ~12-17 calendar days (~2.5-3.5 weeks)** with 2-3 engineers working in parallel. With a single engineer, expect roughly 2-2.5x — **~28-38 calendar days**.

### Days to Production
- Add **P2: 10-15 calendar days** (parallel, 2-3 engineers), largely independent of P1 but P2-1 benefits from P1-1 being done and P2-6 benefits from the P1-3 decision being made.
- **Total: ~22-32 calendar days from today (~4.5-6.5 weeks)** with 2-3 engineers. Single-engineer: **~50-65 calendar days**.

### Confidence level
- **P0: High confidence.** All items are small, well-understood, isolated fixes with clear acceptance criteria (existing tests can be extended directly).
- **P1: Medium confidence.** Engineering effort is well-bounded, but two items carry external/decision risk that can blow the schedule:
  - **P1-1 (TimescaleDB validation)** — the *engineering* work is small, but it's gated on resolving an external Docker Hub/packagecloud access issue that already blocked the team once. If that takes days to resolve (image mirroring, registry auth, etc.), it delays P1-11 and the Limited Beta gate.
  - **P1-13 (signal-confidence decision)** — this is a product/stakeholder decision, not just engineering; calendar time depends on how quickly that conversation happens, not on code.
  - Everything else in P1 is high-confidence given the audit's precise file:line citations.
- **P2: Medium-high confidence.** Mostly mechanical (API contract, batching, test-writing). P2-1 (retention/continuous aggregates) has the most "could take longer than estimated" risk since it's the first real TimescaleDB-specific feature work post-validation.
- **Overall:** The **~3 weeks to Limited Beta / ~6 weeks to Production** estimate (with 2-3 engineers) is realistic *if* P1-1's external blocker is resolved in the first 2-3 days and P1-13's product decision happens in week 1 rather than being deferred. If either of those slips, add 1-2 weeks to the corresponding gate.
