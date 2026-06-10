.PHONY: dev build down feedcheck ingest migrate test fmt lint

# Full dev stack (postgres + redis + app runner) in Docker
dev:
	docker compose -f docker-compose.dev.yml up --build

build:
	docker compose -f docker-compose.dev.yml build

down:
	docker compose -f docker-compose.dev.yml down -v

# Run the feed runner on the host (no Docker). FEED=sim|binance from .env or env.
feedcheck:
	PYTHONPATH=src python -m orvixa.runners.feedcheck

# Run the M2 persistence runner on the host (needs a migrated Postgres).
ingest:
	PYTHONPATH=src python -m orvixa.runners.ingest

# Apply Alembic migrations (DSN comes from Settings/.env, not alembic.ini).
migrate:
	PYTHONPATH=src alembic upgrade head

# Unit + contract + reconnect tests (network tests are opt-in via RUN_NET_TESTS=1,
# DB tests are opt-in via RUN_DB_TESTS=1)
test:
	PYTHONPATH=src pytest -q

fmt:
	ruff format src tests
	ruff check --fix src tests

lint:
	ruff check src tests
	mypy src
