# ORVIXA — Backend (Phase 2)

Crypto market-intelligence platform. **Milestone 1** delivers the market-data
feed layer: a source-agnostic `MarketFeed` interface with two implementations —
a live `BinanceFeed` and an offline, deterministic `SimFeed` — plus a console
runner that proves the feed works. **Milestone 2** adds durable storage:
Alembic-managed TimescaleDB schema, a repository layer, and a batched async
writer that persists every closed candle.

> The symbol manager (M3), indicators/signals (M4) and the API (M5) attach to
> the same two event hooks M1 exposes: `on_candle_close` and
> `on_market_snapshot`. M2 plugs into `on_candle_close` only — the feed layer
> itself is unchanged.

## Structure

```
orvixa/
├─ pyproject.toml            # packaging · ruff · pytest · mypy
├─ alembic.ini, alembic/      # M2 schema migrations (async engine, raw SQL)
│  ├─ env.py
│  └─ versions/0001_initial_schema.py
├─ .env.example              # config template (copy to .env)
├─ docker-compose.dev.yml    # postgres (timescale) + redis + app
├─ Dockerfile.dev            # app image for the dev stack
├─ Makefile                  # dev / feedcheck / ingest / migrate / test / fmt / down
├─ src/orvixa/
│  ├─ config.py              # pydantic-settings, reads .env
│  ├─ logging.py             # structured JSON logging
│  ├─ feeds/
│  │  ├─ base.py             # MarketFeed ABC + Candle / TickerRow
│  │  ├─ sim.py              # SimFeed  (offline, deterministic)
│  │  ├─ binance.py          # BinanceFeed (live WS + REST gap-fill)
│  │  └─ normalize.py        # Binance payloads → internal models
│  ├─ db/
│  │  ├─ pool.py             # asyncpg pool + DBPool protocol
│  │  ├─ models.py           # row dataclasses for the M2 schema
│  │  └─ repository.py       # one repository class per table
│  ├─ persistence/
│  │  ├─ batch_writer.py      # generic size/time-triggered BatchWriter[T]
│  │  ├─ candles.py           # CandleSink: feed candle -> candles table
│  │  └─ registry.py          # symbol-registry seeding (core/alt/meme)
│  └─ runners/
│     ├─ feedcheck.py        # M1: prints candles + a breadth line (no DB)
│     └─ ingest.py           # M2: persists closed candles to Postgres
└─ tests/
   ├─ fixtures/kline_1m.json
   ├─ fake_pool.py           # in-memory DBPool fake for repository tests
   ├─ test_normalize.py
   ├─ test_feed_contract.py
   ├─ test_reconnect.py
   ├─ test_batch_writer.py
   ├─ test_repository.py
   ├─ test_persistence_candles.py
   └─ test_db_integration.py  # opt-in, RUN_DB_TESTS=1, needs real Postgres
```

## Quickstart

```bash
cp .env.example .env          # defaults work out of the box

# Option A — full dev stack (postgres + redis + migrations + ingest) in Docker
make dev

# Option B — run the feed locally against the simulator (no network, no DB)
FEED=sim make feedcheck

# Option C — run the feed locally against live Binance (no DB)
FEED=binance make feedcheck

# Option D — run the M2 persistence pipeline on the host (needs Postgres)
docker compose -f docker-compose.dev.yml up -d postgres
make migrate
make ingest

make test                     # unit + contract + reconnect + persistence tests
```

Binance **public** market-data streams require no API key and no account —
only outbound HTTPS/WSS. M1/M2 are therefore secret-free.

## Definition of done (M1)

- `make dev` brings up postgres + redis + app; live BTC/ETH/SOL 1m candles log within 60s.
- Dropping the network triggers backoff reconnect + REST gap-fill — no missing minutes.
- `FEED=sim` and `FEED=binance` are a pure config swap; no code change.
- Unit + contract + reconnect tests pass; the normalization fixture is locked.
- No DB writes, no HTTP server, no Binance types leak past `feeds/`.

## Definition of done (M2)

- `alembic upgrade head` creates the 8-table schema; `candles`/`indicators` are
  TimescaleDB hypertables with a 7-day compression policy.
- `make ingest` seeds the `symbols` registry, then persists every closed candle
  in batches of `CANDLE_BATCH_MAX_SIZE` / every `CANDLE_BATCH_INTERVAL_SECONDS`,
  whichever comes first.
- A re-delivered or re-backfilled candle for the same `(symbol_id, interval, ts)`
  upserts in place — idempotent under gap-fill and live overlap.
- `FEED=sim` and `FEED=binance` both flow through the same `CandleSink`/
  `BatchWriter` path; `feedcheck` (M1, no DB) is unchanged.
- Repository, batch-writer and candle-sink tests pass with a fake `DBPool` —
  no Docker/Postgres needed for `make test`. A real-database round trip is
  available opt-in via `RUN_DB_TESTS=1` (see `tests/test_db_integration.py`).
