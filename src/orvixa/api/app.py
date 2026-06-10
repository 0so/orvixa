"""FastAPI application — the Phase 2 read-only analytics API.

Endpoints (all read-only, all backed directly by the existing repositories):

* ``GET /symbols``          — every registered symbol from the ``symbols`` registry.
* ``GET /signals/{symbol}`` — recent rows from the ``signals`` log for one symbol.
* ``GET /regime/{symbol}``  — latest market-wide regime snapshot (``market_memory``).
* ``GET /policy/{symbol}``  — policy decisions for one symbol (none are persisted
  in the frozen schema, so this returns ``{}`` — see note below).
* ``GET /health``           — unauthenticated liveness probe.

The policy layer (:mod:`orvixa.backtest.policy_validation`) is a pure,
offline dict-to-dict transform over the validation harness output; it writes
nothing to the database. There is therefore no per-symbol policy table to
read, and ``/policy/{symbol}`` returns an empty object by design. This keeps
the API a faithful, read-only mirror of what the pipeline actually persists
without inventing data or touching core logic.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings, get_settings
from ..db import MarketMemoryRepository, SignalRepository, SymbolRepository
from ..logging import get_logger, setup_logging
from .auth import require_api_key
from .deps import create_readonly_pool, record_to_dict, records_to_list


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    log = get_logger("orvixa.api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await create_readonly_pool(settings)
        app.state.pool = pool
        log.info("api started", extra={"signals_limit": settings.api_signals_limit})
        try:
            yield
        finally:
            await pool.close()
            log.info("api stopped")

    app = FastAPI(
        title="ORVIXA Analytics API",
        version="0.3.0",
        description="Read-only Phase 2 access to signals, regime and policy state.",
        lifespan=lifespan,
    )

    # The static frontend is served from a different origin in dev (or the same
    # one in compose). Allowed origins are explicit and configurable via
    # CORS_ORIGINS; ``Settings`` forbids "*" in production.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    auth = require_api_key(settings)

    async def _symbol_id_or_404(pool, symbol: str) -> int:
        repo = SymbolRepository(pool)
        symbol_id = await repo.get_id(symbol)
        if symbol_id is None:
            raise HTTPException(status_code=404, detail=f"unknown symbol {symbol!r}")
        return symbol_id

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/symbols", tags=["data"], dependencies=[Depends(auth)])
    async def list_symbols() -> list[dict]:
        repo = SymbolRepository(app.state.pool)
        return records_to_list(await repo.list_all())

    @app.get("/signals/{symbol}", tags=["data"], dependencies=[Depends(auth)])
    async def get_signals(symbol: str) -> dict:
        pool = app.state.pool
        symbol_id = await _symbol_id_or_404(pool, symbol)
        repo = SignalRepository(pool)
        rows = await repo.get_recent(symbol_id=symbol_id, limit=settings.api_signals_limit)
        return {"symbol": symbol, "signals": records_to_list(rows)}

    @app.get("/regime/{symbol}", tags=["data"], dependencies=[Depends(auth)])
    async def get_regime(symbol: str) -> dict:
        pool = app.state.pool
        # Validate the symbol exists, then return the latest market-wide regime
        # snapshot. Regime is a whole-market state (`market_memory` has no
        # symbol_id); when none has been computed yet this is an empty object.
        await _symbol_id_or_404(pool, symbol)
        repo = MarketMemoryRepository(pool)
        rows = await repo.get_recent(limit=1)
        latest = record_to_dict(rows[0]) if rows else None
        return {"symbol": symbol, "regime": latest or {}}

    @app.get("/policy/{symbol}", tags=["data"], dependencies=[Depends(auth)])
    async def get_policy(symbol: str) -> dict:
        # Policy decisions are produced offline by the (frozen) validation
        # harness and are not persisted, so there is nothing to read here.
        await _symbol_id_or_404(app.state.pool, symbol)
        return {"symbol": symbol, "policy": {}}

    return app


app = create_app()
