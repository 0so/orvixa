"""Phase 2 API tests — read-only endpoints over a fake pool (no database).

Verifies the documented contract: `/symbols` lists the registry, and
`/regime` returns `{}` when nothing has been computed/persisted. Auth
(X-API-Key) and the unauthenticated `/health` probe are covered too.

30-day Market Intelligence evaluation (frozen 2026-06-12): `/signals` and
`/policy` have been removed from the API surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fake_pool import FakePool
from orvixa.api import app as app_module
from orvixa.config import Settings


@pytest.fixture
def fake_pool() -> FakePool:
    return FakePool()


@pytest.fixture
def client(monkeypatch, fake_pool):
    async def _fake_create(_settings):
        return fake_pool

    monkeypatch.setattr(app_module, "create_readonly_pool", _fake_create)
    settings = Settings(api_key="secret", postgres_dsn="postgresql://x/y")
    app = app_module.create_app(settings)
    with TestClient(app) as c:
        yield c, fake_pool


def test_health_needs_no_auth(client):
    c, _ = client
    assert c.get("/health").json() == {"status": "ok"}


def test_missing_api_key_is_rejected(client):
    c, _ = client
    assert c.get("/symbols").status_code == 401


def test_symbols_lists_registry(client):
    c, pool = client
    pool.fetch_routes["FROM symbols"] = [
        {"id": 1, "base": "BTC_REAL", "symbol": "BTCUSDT", "class": "core"},
        {"id": 2, "base": "ETH_REAL", "symbol": "ETHUSDT", "class": "core"},
    ]
    res = c.get("/symbols", headers={"X-API-Key": "secret"})
    assert res.status_code == 200
    assert [r["base"] for r in res.json()] == ["BTC_REAL", "ETH_REAL"]


def test_regime_empty_object_when_none(client):
    c, pool = client
    pool.fetchval_return = 1
    pool.fetch_routes["FROM market_memory"] = []
    res = c.get("/regime/BTC_REAL", headers={"X-API-Key": "secret"})
    assert res.json() == {"symbol": "BTC_REAL", "regime": {}}


def test_unknown_symbol_is_404(client):
    c, pool = client
    pool.fetchval_return = None  # get_id → None
    res = c.get("/regime/NOPE", headers={"X-API-Key": "secret"})
    assert res.status_code == 404
