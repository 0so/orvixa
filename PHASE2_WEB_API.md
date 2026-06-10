# Phase 2 — Web/API MVP

Read-only web exposure of the frozen Phase 1 analytics stack. **No core logic,
thresholds, pipelines, schema, or CSV data were modified.** Everything here is
additive: a thin FastAPI read layer, daemon wrappers around the existing
runners, a static dashboard, and compose wiring.

## Components added

| Component | Path | Notes |
|-----------|------|-------|
| FastAPI app | `src/orvixa/api/` | Read-only; queries the existing M2 repositories only |
| API-key auth | `src/orvixa/api/auth.py` | `X-API-Key` header; empty key disables auth (dev) |
| Daemon supervisor | `src/orvixa/runners/daemon.py` | `while True: run(); sleep(interval)` around the unchanged `ingest.run` / `analytics.run` |
| Frontend SPA | `frontend/index.html` | Vanilla JS, polls every 7 s; signals / regime / policy panels |
| Compose services | `docker-compose.dev.yml` | `migrate`, `ingest`, `analytics`, `api`, `frontend`, optional `backfill` |

## Reused as-is (unchanged)

- `analytics/` (engine, signals, regime, indicators, events) — untouched.
- `backtest/` validation/policy harness — untouched.
- `db/repository.py`, `db/models.py`, Alembic migrations (`0001`, `0002`).
- `runners/ingest.py`, `runners/analytics.py`, `runners/backfill.py` — called, not edited.

## Endpoints

All under API-key auth except `/health`. Interactive docs at `/docs`.

| Method | Path | Returns |
|--------|------|---------|
| GET | `/health` | `{"status":"ok"}` (no auth — container probe) |
| GET | `/symbols` | full `symbols` registry (`BTC_REAL`, `ETH_REAL`, …) |
| GET | `/signals/{symbol}` | `{"symbol", "signals": [...]}` — recent `signals` rows |
| GET | `/regime/{symbol}` | `{"symbol", "regime": {...}}` — latest market-wide `market_memory` snapshot, or `{}` |
| GET | `/policy/{symbol}` | `{"symbol", "policy": {}}` — see note |

**Policy note:** the policy layer (`backtest/policy_validation.py`) is a pure,
offline dict-to-dict transform over the validation harness and persists
nothing. There is no policy table in the frozen schema, so `/policy/{symbol}`
returns `{}` by design — the API stays a faithful mirror of what the pipeline
actually stores rather than recomputing decisions on the read path.

## Running the stack

```bash
cp .env.example .env          # set API_KEY
docker compose -f docker-compose.dev.yml up --build
# api:      http://localhost:8000  (/docs for OpenAPI)
# frontend: http://localhost:8080  (enter API base + key in the header)
```

`migrate` runs `alembic upgrade head` once; `ingest`/`analytics` start as
restart-on-exit daemons; `api` + `frontend` come up after migrations.

Optional historical reload (idempotent — `insert_batch` upserts on
`(symbol_id, interval, ts)`):

```bash
docker compose -f docker-compose.dev.yml --profile backfill up backfill
```

## Verification

- `make test` / `pytest -q` → **164 passed, 1 skipped** (incl. `tests/test_api.py`).
- `ruff check` clean on the new modules.
- Documented read-only contract verified in `tests/test_api.py`:
  - `/symbols` → registry list
  - `/signals/BTC_REAL` → `{"symbol":"BTC_REAL","signals":[]}` (empty, expected)
  - `/regime/BTC_REAL` → `{"symbol":"BTC_REAL","regime":{}}`
  - `/policy/BTC_REAL` → `{"symbol":"BTC_REAL","policy":{}}`
  - unknown symbol → `404`; missing API key → `401`
- Read-only: the API issues only `SELECT` (no `INSERT`/`UPDATE`/DDL); it opens
  its own pool with a `jsonb` decode codec and never imports a write path.
- Reproducible & idempotent: backfill re-runs are upserts; the schema and CSVs
  are untouched; the stack is fully described by `docker-compose.dev.yml`.
