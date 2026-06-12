"""FastAPI application — the Phase 2 read-only analytics API.

Endpoints (all read-only, all backed directly by the existing repositories):

* ``GET /symbols``         — every registered symbol from the ``symbols`` registry.
* ``GET /regime/{symbol}`` — latest market-wide regime snapshot (``market_memory``).
* ``GET /health``          — unauthenticated liveness probe.

30-day Market Intelligence evaluation (frozen 2026-06-12): the BUY/SELL/
HIGHVOL signal engine and the (always-empty) policy endpoint are not part of
the visible/active product surface during this window, so ``/signals/{symbol}``
and ``/policy/{symbol}`` have been removed from the API. The ``signals`` table
and repository still exist in the schema/codebase; only the API surface and
the analytics engine's signal evaluation (gated by ``Settings.enable_signals``)
are disabled.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings, get_settings
from ..db import MarketMemoryRepository, SymbolRepository
from ..logging import get_logger, setup_logging
from .auth import require_api_key
from .deps import create_readonly_pool, record_to_dict, records_to_list


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    log = get_logger("orvixa.api")

    if settings.app_env.lower() != "production":
        log.warning(
            "RUNNING IN DEV MODE — auth may be disabled and CORS may be open; "
            "do not expose this instance publicly",
            extra={"app_env": settings.app_env},
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await create_readonly_pool(settings)
        app.state.pool = pool
        log.info("api started")
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

    return app


app = create_app()
