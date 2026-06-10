# M2 Delivery Report — Persistence (Postgres / TimescaleDB)

Status: **complete**, scoped strictly to the approved M2 plan ("Durable
storage for candles, events, signals and snapshots with batched, non-blocking
writes"). M1's feed layer, `feedcheck` runner, and the ORVIXA UI are
untouched. Milestone 3 has not been started.

## 1. Implementation summary

### Schema (Alembic, `alembic/versions/0001_initial_schema.py`)

The full 8-table schema from the approved architecture ("05 Database
schema"):

- `symbols` — registry, FK target for everything else (`symbol`/`base`/
  `quote`/`class`/`tier`/`status`/`tags`/`first_seen`).
- `candles` — TimescaleDB hypertable on `ts`, PK `(symbol_id, interval, ts)`,
  compression policy (`compress_segmentby = symbol_id, interval`,
  `compress_orderby = ts DESC`, chunks > 7 days compressed).
- `indicators` — TimescaleDB hypertable on `ts`, PK `(symbol_id, ts)`, same
  7-day compression policy.
- `signals`, `market_events`, `market_memory`, `market_reports`,
  `telegram_alerts` — bigserial logs with the columns/check-constraints from
  the architecture doc; `telegram_alerts` has a partial unique index on
  `dedupe_key` to back the repository's `ON CONFLICT ... DO NOTHING`.

Migrations are raw SQL via `op.execute` (no ORM/model duplication), run
through an async SQLAlchemy engine in `alembic/env.py`. The DSN comes from
`Settings.postgres_dsn` (same source as the app pool), not `alembic.ini`.

### Data layer (`src/orvixa/db/`)

- `pool.py` — `DBPool` Protocol (the asyncpg `Pool` subset repos use) +
  `create_pool(settings)`.
- `models.py` — one `slots=True` dataclass per table (`SymbolRow`,
  `CandleRow`, `IndicatorRow`, `SignalRow`, `MarketEventRow`,
  `MarketMemoryRow`, `MarketReportRow`, `TelegramAlertRow`).
- `repository.py` — one repository class per table. `CandleRepository
  .insert_batch` is an idempotent upsert keyed on `(symbol_id, interval, ts)`
  so re-delivered/re-backfilled candles overwrite rather than duplicate.
  `SymbolRepository.upsert` is keyed on `base` (the canonical display
  symbol).

### Persistence pipeline (`src/orvixa/persistence/`)

- `batch_writer.py` — generic `BatchWriter[T]`: buffers items, flushes on
  `max_size` **or** `interval_seconds` (whichever first), graceful `stop()`
  drains the buffer, sink errors are caught/logged/counted (never crash the
  loop).
- `candles.py` — `CandleSink.handle_candle` is registered on
  `feed.on_candle_close`; ignores in-progress candles, resolves
  `Candle.symbol` ("BTC") to `symbols.id` via `SymbolRepository.get_id`
  (cached), builds a `CandleRow`, and hands it to the `BatchWriter`.
  `candle_repository_sink` adapts `CandleRepository.insert_batch` to the
  writer's sink shape.
- `registry.py` — `seed_symbols`/`build_symbol_rows`: classifies
  `settings.all_symbols` into `core` (configured `core_symbols`), `meme`
  (curated DOGE/SHIB/PEPE/WIF/BONK/FLOKI), or `alt`, and upserts them.

### New runner (`src/orvixa/runners/ingest.py`, `orvixa-ingest` script)

Builds the feed via the existing `factory.build_feed()` (unchanged
`FEED=sim|binance` switch), seeds the symbol registry, wires
`on_candle_close` → `CandleSink` → `BatchWriter` → `CandleRepository
.insert_batch`. `feedcheck` (M1) is untouched and still has zero DB
dependency.

### Backward-compatible `Candle` extension

Added `Candle.taker_buy_volume: float = 0.0` (additive, default-valued —
existing positional construction still works) so the feed layer can supply
`candles.taker_buy_v`. Populated in `normalize.py` (Binance kline/REST) and
`sim.py` (derived from the simulated bar's bias).

### Config (`src/orvixa/config.py`)

Added M2 settings: `db_pool_min_size`/`db_pool_max_size`,
`candle_batch_max_size` (200), `candle_batch_interval_seconds` (2.0) — meets
the "1-5s batches" done-criteria. `postgres_dsn`/`redis_url` retained.

### Tooling

- `pyproject.toml`: added `asyncpg`, dev-only `alembic` + `sqlalchemy[asyncio]`,
  new `orvixa-ingest` script entry point.
- `Makefile`: new `ingest` and `migrate` targets.
- `docker-compose.dev.yml` / `Dockerfile.dev`: `app` service now runs
  `alembic upgrade head && python -m orvixa.runners.ingest`.
- `.env.example`: documents the new pool/batch settings.
- `README.md`: M2 structure, quickstart, and definition-of-done sections.

## 2. Migration summary

| Revision | Description |
|---|---|
| `0001_initial_schema` | Creates `timescaledb` extension, all 8 tables, hypertables for `candles`/`indicators`, 7-day compression policies, indexes (`signals`, `market_events`, `market_memory`, `market_reports`, `telegram_alerts`), and the partial unique index for `telegram_alerts.dedupe_key`. |

Verified offline: `PYTHONPATH=src alembic history` and
`PYTHONPATH=src alembic upgrade head --sql` both run cleanly and emit valid
DDL (`alembic_version` table + the full `0001` script). No live Postgres is
available in this sandbox (no Docker daemon), so the migration has not been
applied to a real database — see the readiness review below.

## 3. Test results

```
$ make test
42 passed, 1 skipped in ~1s
```

New M2 test files (all DB-free, using `tests/fake_pool.py`'s in-memory
`DBPool` fake — same pattern as M1's fake WebSocket connector):

- `tests/test_batch_writer.py` — size-triggered flush, interval-triggered
  flush, graceful drain on `stop()`, `add_many` size trigger, sink-error
  isolation/counting, idempotent start/stop.
- `tests/test_repository.py` — all 8 repositories: SQL shape
  (`ON CONFLICT`/`RETURNING`/table names), parameter passing, return-value
  parsing, including the `telegram_alerts` dedupe-conflict (`-1`) path.
- `tests/test_persistence_candles.py` — `CandleSink` (ignores unclosed
  candles, cache-hit vs. repository-lookup symbol resolution, drops candles
  for unknown symbols), `candle_repository_sink`, an end-to-end
  `BatchWriter` + `CandleSink` flush, and `build_symbol_rows` core/alt/meme
  classification.
- `tests/test_db_integration.py` — **opt-in** (`RUN_DB_TESTS=1`), skipped by
  default; round-trips a candle through a real Postgres/TimescaleDB once
  migrated. Skipped in this run (1 skip above) — no Docker daemon in this
  sandbox.

`ruff check src tests` and `mypy src` are clean except two pre-existing
`ASYNC110` lint findings in `tests/test_feed_contract.py` /
`tests/test_reconnect.py` (M1 fakes using `while ... await asyncio.sleep`),
unrelated to M2 and unchanged by this work.

## 4. Readiness review

| Item | Status | Notes |
|---|---|---|
| 7-table schema (8 incl. registry) per architecture | ✅ | `alembic/versions/0001_initial_schema.py` |
| Hypertables + compression for candles/indicators | ✅ | `create_hypertable` + `add_compression_policy(... '7 days')` |
| Async batched writer (1-5s) | ✅ | `BatchWriter`, default `max_size=200` / `interval=2.0s` |
| Repository layer | ✅ | `db/repository.py`, 8 classes |
| `FEED=sim`/`FEED=binance` unchanged | ✅ | `factory.build_feed` untouched; `ingest.py` is additive |
| Idempotent candle upsert (gap-fill safe) | ✅ | `ON CONFLICT (symbol_id, interval, ts) DO UPDATE` |
| Symbol registry seeding | ✅ | `persistence/registry.py`, core/alt/meme heuristic |
| Tests for all persistence components | ✅ | 21 new tests, DB-free |
| Live DB verification (`alembic upgrade head`, real INSERT/SELECT, compression active) | ⚠️ N/A | No Docker/Postgres in this sandbox — opt-in `test_db_integration.py` written but not executed; offline SQL generation verified instead |
| ORVIXA UI unchanged | ✅ | no UI files touched |

**Outstanding before this is fully proven end-to-end:** run
`docker compose -f docker-compose.dev.yml up -d postgres`, `make migrate`,
then `RUN_DB_TESTS=1 pytest tests/test_db_integration.py` and `make ingest`
against `FEED=sim`/`FEED=binance` to confirm live candles land in the
`candles` table within the 1-5s batch window and that
`SELECT * FROM candles ORDER BY ts DESC LIMIT N` returns them — this is
infrastructure verification, not a code gap.

## 5. Go/no-go for Milestone 3

**GO**, conditional on the live-DB smoke test above (Docker/Postgres
available in the target environment, which this sandbox lacks). The
persistence seam (`CandleSink` → `BatchWriter` → `CandleRepository`) and the
full repository layer for `indicators`/`signals`/`market_events` are in place
and tested, ready for M3 (symbol manager) and M4 (indicators/signals) to
build on without further schema changes.
