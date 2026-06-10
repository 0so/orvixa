"""Tests for :mod:`orvixa.symbols.client` — no network, fake HTTP client."""

from __future__ import annotations

from typing import Any

from orvixa.symbols.client import BinanceMarketClient


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    async def __aenter__(self) -> _FakeHTTPClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requested.append(url)
        return _FakeResponse(self._responses[url])


_EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "isSpotTradingAllowed": True,
        },
        {
            "symbol": "ETHUSDT",
            "baseAsset": "ETH",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "isSpotTradingAllowed": True,
        },
        # delisted/halted pair: status BREAK
        {
            "symbol": "OLDUSDT",
            "baseAsset": "OLD",
            "quoteAsset": "USDT",
            "status": "BREAK",
            "isSpotTradingAllowed": True,
        },
        # non-USDT quote: must be filtered out
        {
            "symbol": "BTCBUSD",
            "baseAsset": "BTC",
            "quoteAsset": "BUSD",
            "status": "TRADING",
            "isSpotTradingAllowed": True,
        },
        # margin-only: not spot tradable
        {
            "symbol": "MARGINUSDT",
            "baseAsset": "MARGIN",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "isSpotTradingAllowed": False,
        },
    ]
}

_TICKER_24HR = [
    {"symbol": "BTCUSDT", "lastPrice": "68000.5", "quoteVolume": "1000000", "priceChangePercent": "1.5", "count": 50000},
    {"symbol": "ETHUSDT", "lastPrice": "3500.2", "quoteVolume": "500000", "priceChangePercent": "-2.1", "count": 30000},
    # non-USDT pair must be filtered out
    {"symbol": "ETHBTC", "lastPrice": "0.05", "quoteVolume": "100", "priceChangePercent": "0.1", "count": 100},
    # malformed entry must be skipped, not fatal
    {"symbol": "BROKENUSDT", "lastPrice": "not-a-number", "quoteVolume": "1", "priceChangePercent": "0", "count": 1},
]


def _make_client() -> BinanceMarketClient:
    fake = _FakeHTTPClient(
        {
            "/api/v3/exchangeInfo": _EXCHANGE_INFO,
            "/api/v3/ticker/24hr": _TICKER_24HR,
        }
    )
    return BinanceMarketClient(client_factory=lambda: fake)


async def test_fetch_exchange_info_filters_to_tradable_usdt_spot() -> None:
    client = _make_client()
    symbols = await client.fetch_exchange_info()

    pairs = {s.pair for s in symbols}
    assert pairs == {"BTCUSDT", "ETHUSDT", "OLDUSDT"}

    by_pair = {s.pair: s for s in symbols}
    assert by_pair["BTCUSDT"].status == "TRADING"
    assert by_pair["BTCUSDT"].base == "BTC"
    assert by_pair["OLDUSDT"].status == "BREAK"


async def test_fetch_ticker_24hr_filters_and_skips_malformed() -> None:
    client = _make_client()
    tickers = await client.fetch_ticker_24hr()

    assert set(tickers) == {"BTCUSDT", "ETHUSDT"}
    btc = tickers["BTCUSDT"]
    assert btc.base == "BTC"
    assert btc.quote_volume == 1_000_000.0
    assert btc.price_change_pct == 1.5
    assert btc.count == 50_000
