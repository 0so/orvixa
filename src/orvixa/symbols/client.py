"""REST client for Binance's whole-market discovery endpoints.

The Symbol Manager (M3) needs the *entire* USDT spot universe and its 24h
activity to discover new listings/delistings and rank symbols — neither of
which the M1 ``BinanceFeed`` (a per-symbol WebSocket feed) provides. This
module is the only place that knows the ``/exchangeInfo`` and
``/ticker/24hr`` payload shapes.

Testability mirrors ``BinanceFeed``: the HTTP client is injected via
``client_factory`` so the manager runs with no network in tests.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from .models import ExchangeSymbol, TickerStats

logger = logging.getLogger("orvixa.symbols.client")

_ACTIVE_EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
_TICKER_24HR_PATH = "/api/v3/ticker/24hr"


class _HTTPResponse(Protocol):
    def raise_for_status(self) -> object: ...
    def json(self) -> Any: ...


class _HTTPClient(Protocol):
    async def get(self, url: str, **kwargs: object) -> _HTTPResponse: ...
    async def __aenter__(self) -> _HTTPClient: ...
    async def __aexit__(self, *exc: object) -> None: ...


ClientFactory = Callable[[], _HTTPClient]
AsyncClientFactory = Callable[[], Awaitable[_HTTPClient]]


class BinanceMarketClient:
    """Fetches the tradable USDT spot universe and its 24h ticker stats."""

    def __init__(
        self,
        rest_base: str = "https://api.binance.com",
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._rest_base = rest_base.rstrip("/")
        self._client_factory = client_factory or self._default_client_factory

    def _default_client_factory(self) -> _HTTPClient:
        return httpx.AsyncClient(base_url=self._rest_base, timeout=10.0)

    async def fetch_exchange_info(self) -> list[ExchangeSymbol]:
        """All USDT spot pairs (any status) from ``/exchangeInfo``."""
        async with self._client_factory() as client:
            resp = await client.get(_ACTIVE_EXCHANGE_INFO_PATH)
            resp.raise_for_status()
            data = resp.json()

        out: list[ExchangeSymbol] = []
        for item in data.get("symbols", []):
            try:
                if str(item["quoteAsset"]).upper() != "USDT":
                    continue
                if not item.get("isSpotTradingAllowed", False):
                    continue
                out.append(
                    ExchangeSymbol(
                        pair=str(item["symbol"]).upper(),
                        base_asset=str(item["baseAsset"]).upper(),
                        quote_asset=str(item["quoteAsset"]).upper(),
                        status=str(item.get("status", "TRADING")).upper(),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    async def fetch_ticker_24hr(self) -> dict[str, TickerStats]:
        """24h stats for every USDT pair, keyed by pair (e.g. ``"BTCUSDT"``)."""
        async with self._client_factory() as client:
            resp = await client.get(_TICKER_24HR_PATH)
            resp.raise_for_status()
            data = resp.json()

        out: dict[str, TickerStats] = {}
        for item in data:
            try:
                pair = str(item["symbol"]).upper()
                if not pair.endswith("USDT"):
                    continue
                out[pair] = TickerStats(
                    pair=pair,
                    last_price=float(item["lastPrice"]),
                    quote_volume=float(item["quoteVolume"]),
                    price_change_pct=float(item["priceChangePercent"]),
                    count=int(item["count"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out
