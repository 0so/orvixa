# Reproducibility Notes — Phase 1 Snapshot

## Dependencies / Runtime Requirements

- Python 3.11+
- PostgreSQL 16 with the **TimescaleDB** extension (>=2.x). The provided
  `docker-compose.dev.yml` uses `timescale/timescaledb:2.17.2-pg16`.
  - If running Postgres natively instead of via Docker, the
    `timescaledb-2-postgresql-16` package (from the TimescaleDB apt repo)
    must be installed and `shared_preload_libraries = 'timescaledb'` set in
    `postgresql.conf`, then the server restarted.
- Docker + Docker Compose (for `docker-compose.dev.yml`), if using the
  containerized stack.
- Project Python dependencies: `pip install -e ".[dev]"` (see
  `pyproject.toml`).

No code, threshold, schema, or CSV data was modified for this snapshot
beyond the timestamp-unit fix already applied to
`data/real/BTC_REAL.csv` / `data/real/ETH_REAL.csv` (epoch-µs → epoch-ms),
which is included in this snapshot's `data/real/`.

## How to Start the System

### Option A — Docker Compose
```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up -d postgres redis
```

### Option B — Local Postgres (if Docker images are unavailable)
```bash
cp .env.example .env
# edit .env: POSTGRES_DSN=postgresql://orvixa:orvixa@localhost:5432/orvixa
sudo service postgresql start
sudo -u postgres psql -c "CREATE USER orvixa WITH PASSWORD 'orvixa' SUPERUSER;"
sudo -u postgres psql -c "CREATE DATABASE orvixa OWNER orvixa;"
# install + enable timescaledb extension as described above
```

## How to Run the Full Pipeline From Scratch

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

export $(cat .env | grep -v '^#' | xargs)

# 1. Apply migrations
python -m alembic upgrade head

# 2. Register the BTC_REAL / ETH_REAL symbols (one-time)
python3 - <<'EOF'
import asyncio
from orvixa.config import Settings
from orvixa.db.pool import create_pool
from orvixa.db.repository import SymbolRepository
from orvixa.db.models import SymbolRow

async def main():
    pool = await create_pool(Settings())
    repo = SymbolRepository(pool)
    for base in ("BTC_REAL", "ETH_REAL"):
        await repo.upsert(SymbolRow(symbol=base, base=base, quote="USDT", klass="core", tier=0, tags=[]))
    await pool.close()

asyncio.run(main())
EOF

# 3. Backfill candles (idempotent — safe to re-run)
orvixa-backfill data/real --interval 1m

# 4. Run the validation stack
python3 - <<'EOF'
import asyncio, json
from orvixa.config import Settings
from orvixa.db.pool import create_pool
from orvixa.backtest import run_policy_validation

async def main():
    pool = await create_pool(Settings())
    result = await run_policy_validation(pool, Settings(), ["BTC_REAL", "ETH_REAL"], mode="edge_evaluation")
    print(json.dumps({
        "dataset_type": result["dataset_type"],
        "signal_counts": {k: len(v) for k, v in result["signals"].items()},
        "regime_metrics": result["regime_metrics"],
        "policy_decisions": result["policy_decisions"],
    }, indent=2, default=str))
    await pool.close()

asyncio.run(main())
EOF
```

## Expected Outputs

- Alembic: migrates to `0002 (head)` with no errors.
- Backfill: `BTC_REAL` and `ETH_REAL` each receive 44,640 candle rows
  (2026-05-01T00:00 → 2026-05-31T23:59, 1m interval). Re-running the
  backfill is idempotent (upsert on `(symbol_id, interval, ts)`).
- `classify_dataset` → `"REAL"`.
- `run_signal_validation(mode="edge_evaluation")` runs without error.
- Signal counts: `BTC_REAL=0`, `ETH_REAL=0`.
- `regime_metrics` and `policy_decisions`: `{}` for both symbols.

These outputs are documented in full in `PHASE1_FINAL_RESULTS.md` and
`PHASE1_AUDIT_REPORT.md`, both included in this snapshot.
