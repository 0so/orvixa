# M2 Validation Report — Persistence (Postgres / TimescaleDB)

Status: **validation complete**. This report covers the live-DB validation
requested as a follow-up to `M2_DELIVERY_REPORT.md` (which was GO,
conditional on this smoke test). Milestone 3 has **not** been started — this
report is research/validation only, scoped to the 10 items requested.

## 0. Environment notes (read first)

The target sandbox has **no Docker daemon running by default** and **no
TimescaleDB packages available** (Docker Hub anonymous pull rate limit, and
TimescaleDB's apt repo returns 403). Both are environment/infrastructure
constraints, not code defects. To still exercise the real `orvixa` code
against a real Postgres:

- `dockerd` was started manually; `docker compose -f docker-compose.dev.yml
  up -d --build` was attempted but failed immediately on `timescale/
  timescaledb:2.17.2-pg16` and `redis:7-alpine` with "You have reached your
  unauthenticated pull rate limit" (confirmed via repeated retries across the
  session, including a final retry just before writing this report — still
  rate-limited).
- **Substitute stack**: native PostgreSQL 16 + Redis installed via `apt`
  (`postgresql-16`, `redis-server`), started with `service ... start`.
- **Schema**: generated the real migration SQL via `PYTHONPATH=src alembic
  upgrade head --sql` (the same code path/migration used by `make migrate`),
  then stripped the TimescaleDB-only statements (`CREATE EXTENSION
  timescaledb`, `create_hypertable`, `add_compression_policy`, `ALTER TABLE
  ... SET (timescaledb.compress...)`) and applied the rest via `psql`. This
  validates all 8 application tables, FKs, constraints and indexes for real;
  the hypertable/compression DDL itself remains verified only via offline SQL
  generation (done in M2 delivery), not live execution.
- `.env` was copied from `.env.example` and adjusted only for this
  validation: `FEED=binance` → `FEED=sim` (live Binance returns 403 in this
  sandbox) and `POSTGRES_DSN` repointed at `localhost` instead of the
  docker-compose `postgres` host. **`.env.example` itself was not changed.**

None of the application code paths exercised below differ between this
substitute stack and the docker-compose stack — the same `alembic`,
`orvixa.db`, `orvixa.persistence`, and `orvixa.runners.ingest` modules ran
unmodified against a real `asyncpg` pool.

## 1. Task-by-task results

### 1. Start the complete stack
**Partial / substituted.** `docker compose up` blocked by Docker Hub rate
limiting (infrastructure, not code). Substituted native Postgres 16 + Redis,
both started successfully. App code (`orvixa-ingest`) ran directly on the
host against this substitute stack.

### 2. Run migrations
**Pass (with caveat).** `PYTHONPATH=src alembic upgrade head --sql` generates
valid DDL for revision `0001` (`alembic_version` table, all 8 tables,
hypertables, compression policies, indexes). The non-Timescale portion was
applied live via `psql` and succeeded cleanly (`COMMIT`). The
Timescale-specific statements (`create_hypertable`, compression policies)
were **not** applied live in this session because the extension isn't
installable here — they were verified offline only (consistent with the M2
delivery report).

### 3. Verify TimescaleDB tables exist
**Pass for table structure; Timescale-feature caveat as above.** All 8
tables + `alembic_version` (9 total) exist with correct columns, types,
defaults, CHECK constraints, FKs, PK `(symbol_id, interval, ts)` on
`candles`, and all named indexes (`signals_symbol_ts_idx`,
`market_events_symbol_ts_idx`, `market_memory_ts_idx`,
`market_reports_ts_idx`, `telegram_alerts_ts_idx`,
`telegram_alerts_dedupe_key_uniq` partial unique index). `create_hypertable`
and `add_compression_policy` calls were not exercised live (no `timescaledb`
extension available); confirmed only via offline SQL generation.

### 4. Verify ingestion writes real candles
**Pass.** Ran `orvixa-ingest` (FEED=sim) for two sessions (~25s and ~108s).
Real OHLCV/quote-volume/trades/taker-buy-volume rows landed in `candles` for
all 11 configured symbols, with plausible simulated values. Run 1: 99
candles. Run 2 (longer): 396 candles.

### 5. Verify symbols are inserted correctly
**Pass.** `seed_symbols` populated all 11 symbols from `settings.all_symbols`
with the expected core/alt/meme classification:
- `core`/tier 0: BTC, ETH, SOL
- `alt`/tier 1: BNB, XRP, AVAX, LINK
- `meme`/tier 1: DOGE, PEPE, SHIB, WIF

Re-running ingest a second time left the registry at 11 rows (idempotent
upsert keyed on `base`, no duplicates).

### 6. Verify batch writers flush correctly
**Pass.** Over a 108s run with `CANDLE_BATCH_MAX_SIZE=200` /
`CANDLE_BATCH_INTERVAL_SECONDS=2.0` (defaults), the writer logged
`flush_count=36`, consistent with the ~3s simulated candle-close cadence
driving interval-triggered flushes (well within the 1-5s done-criteria). No
sink errors (`error_count=0`).

### 7. Verify no duplicate candle insertion
**Pass.** `SELECT count(*), count(DISTINCT (symbol_id, ts))` over the 396
ingested rows for the run returned equal counts (396 == 396) — no duplicate
`(symbol_id, interval, ts)` keys. Additionally, `tests/
test_db_integration.py::test_candle_round_trip` explicitly re-inserts the
same `CandleRow` and confirms the row count for that exact key stays at 1
(upsert overwrites, doesn't duplicate) — see fix below.

### 8. Verify reconnect does not create gaps
**Pass.** Built a standalone validation script (`/tmp/
validate_reconnect.py`) using the real `BinanceFeed`, `CandleSink`,
`BatchWriter`, and `CandleRepository` against the live Postgres instance,
with a fake WebSocket connector (first attempt fails, second succeeds) and a
fake REST backfiller (returns minutes T and T+1 on reconnect gap-fill). The
live socket then delivers an *overlapping* re-close for T+1 (different
close price) plus a brand-new minute T+2.

Result:
- `connect attempts: 2`, `backoff history: [1.19s]`, `gapfill calls: 1`
- 3 rows persisted for minutes T, T+1, T+2 — no gap, no duplicate timestamps
- The T+1 row reflects the **live** re-close value (101.9), not the stale
  gap-fill value (101.5) — proves the reconnect/gap-fill/live-overlap upsert
  path is correct end-to-end.

### 9. Verify memory usage remains stable
**Pass.** Sampled `VmRSS` of the `orvixa-ingest` process via `/proc/$PID/
status` over ~96s of continuous ingestion (396 candles, 36 flushes):
41848 KB → 41928 KB, a <0.2% change with no monotonic growth trend — no
evidence of a leak in the batch-writer/candle-sink/repository path over this
window.

### 10. Production readiness report
This document. See GO/NO-GO below.

## 2. Discovered issues

| # | Issue | Severity | Status |
|---|---|---|---|
| 1 | Docker Hub anonymous pull rate limit prevents `docker compose up` for `timescale/timescaledb` and `redis:7-alpine` images in this sandbox. | Environment | Not fixable in-session; not a code defect. Substitute stack used for validation. |
| 2 | TimescaleDB extension not installable in this sandbox (packagecloud.io repo returns 403). Hypertable/compression DDL not exercised live. | Environment | Not fixable in-session. Offline SQL generation (`alembic upgrade head --sql`) confirms the DDL is syntactically valid; recommend running `alembic upgrade head` against the real docker-compose Postgres (with Timescale) in an environment with registry access before production use. |
| 3 | `tests/test_db_integration.py::test_candle_round_trip` failed (`assert len(recent_again) == 1` → got 10) when run against a DB that already had rows from prior validation runs. Test bug: `get_recent(symbol_id, limit=10)` returns up to the 10 most-recent rows for the *whole* symbol, not just the freshly-inserted one. | Low (test-only, no product bug) | **Fixed** — see below. |
| 4 | `candles` numeric columns (`o`/`h`/`l`/`c`/`v`/`quote_v`/`taker_buy_v`) use `numeric` with no precision/scale, so float inputs are stored with full binary-float artifacts (e.g. `100.50000000000001` style values possible). Cosmetic/data-quality observation, not exercised as a failure in this validation. | Cosmetic | Not fixed — flagging for M3+ schema consideration if exact-decimal display matters. |
| 5 | `orvixa-ingest` run under `timeout 25 ... | tee ...` exited with code 143 ("Terminated") without logging the graceful "ingest stopped" summary, whereas the same process killed via `kill -TERM $PID` directly (via `nohup`) shut down gracefully and logged full stats. Likely an artifact of how `timeout`'s SIGTERM is delivered through a piped foreground process group, not a flaw in `ingest.py`'s signal handling (confirmed graceful shutdown works when signaled directly). | Low / observational | Not fixed — recommend testing graceful shutdown via `docker compose stop` (SIGTERM to PID 1) once the real stack is available, to confirm this isn't latent. |

## 3. Fixes applied

### `tests/test_db_integration.py` — re-delivery upsert assertion

**Before** (only valid against an empty `candles` table):
```python
recent = await candle_repo.get_recent(symbol_id, limit=1)
assert len(recent) == 1
assert recent[0]["symbol_id"] == symbol_id

await candle_repo.insert_batch([row])
recent_again = await candle_repo.get_recent(symbol_id, limit=10)
assert len(recent_again) == 1
```

**After** (counts the exact `(symbol_id, interval, ts)` key before/after
re-insertion — correct regardless of how many other rows exist for the
symbol):
```python
recent = await candle_repo.get_recent(symbol_id, limit=1)
assert len(recent) == 1
assert recent[0]["symbol_id"] == symbol_id
assert recent[0]["ts"] == row.ts

# Re-delivery upserts in place rather than duplicating: the row count
# for this exact (symbol_id, interval, ts) stays at one.
before = await pool.fetchval(
    "SELECT count(*) FROM candles WHERE symbol_id = $1 AND interval = $2 AND ts = $3",
    symbol_id, row.interval, row.ts,
)
await candle_repo.insert_batch([row])
after = await pool.fetchval(
    "SELECT count(*) FROM candles WHERE symbol_id = $1 AND interval = $2 AND ts = $3",
    symbol_id, row.interval, row.ts,
)
assert before == 1
assert after == 1
```

Verified: `RUN_DB_TESTS=1 PYTHONPATH=src pytest -q` → **43 passed**. Without
the flag: **42 passed, 1 skipped** (unchanged from M2 delivery). `ruff check
src tests` and `mypy src` remain clean (only the 2 pre-existing,
M2-unrelated `ASYNC110` findings in M1 test fakes).

## 4. GO / NO-GO decision for Milestone 3

**GO.**

All 9 functional checks (items 2-9) pass against a real Postgres database
running the real `orvixa` migration, repository, persistence, and feed code
— including the previously-untested reconnect/gap-fill/upsert path and a
memory-stability run. The one bug found was in the opt-in integration test
itself (now fixed, full suite green).

The two open items (Docker Hub rate limit, TimescaleDB extension
unavailability) are sandbox/registry constraints external to the codebase.
They do not block M3 development, which builds on the repository/persistence
layer validated here rather than on hypertable-specific behavior. Recommend,
as a follow-up (not blocking M3 start), running `docker compose -f
docker-compose.dev.yml up -d && make migrate` once in an environment with
registry access, to confirm `create_hypertable`/compression policies apply
cleanly against the real `timescale/timescaledb` image — this closes the
last residual gap from the M2 delivery report's readiness review.

Milestone 3 has **not** been started, per instructions.
