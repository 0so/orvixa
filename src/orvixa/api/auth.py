"""Minimal API-key authentication for the Phase 2 read-only API.

A single shared key, supplied via the ``API_KEY`` setting and presented
by clients in the ``X-API-Key`` header. No JWT, no users, no sessions — just
enough to keep the read-only dashboard endpoints from being wide open. The
``/health`` probe and the OpenAPI docs are intentionally left unauthenticated.
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from ..config import Settings


def require_api_key(settings: Settings):
    """Build a FastAPI dependency that enforces the configured ``X-API-Key``.

    ``Settings`` validation already guarantees that, in production, ``api_key``
    is non-empty and not the checked-in default — so an empty key here only
    occurs in explicit ``app_env=development`` setups, where auth is disabled
    for local convenience.
    """

    async def _check(x_api_key: str | None = Header(default=None)) -> None:
        expected = settings.api_key
        if not expected:
            return
        if x_api_key != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid API key (X-API-Key header)",
            )

    return _check
