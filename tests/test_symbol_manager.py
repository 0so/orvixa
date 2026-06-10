"""Tests for :class:`orvixa.symbols.manager.SymbolManager` — no live Binance.

A fake market client supplies scripted ``/exchangeInfo`` + ``/ticker/24hr``
"cycles" (one per ``refresh_universe()`` call), a fake feed records
subscribe/unsubscribe calls, and :class:`fake_pool.FakePool` stands in for
Postgres — mirroring the M1/M2 fake-driven test patterns.
"""

from __future__ import annotations

import json

from fake_pool import FakePool
from orvixa.config import Settings
from orvixa.db.repository import SymbolRepository
from orvixa.feeds.base import TickerRow
from orvixa.symbols.manager import SymbolManager
from orvixa.symbols.models import ExchangeSymbol, TickerStats


class _FakeFeed:
    def __init__(self) -> None:
        self.subscribed: set[str] = set()
        self.subscribe_calls: list[set[str]] = []
        self.unsubscribe_calls: list[set[str]] = []
        self._snapshot_cbs: list = []

    def on_market_snapshot(self, cb) -> None:
        self._snapshot_cbs.append(cb)

    async def subscribe(self, symbols) -> None:
        s = {x.upper() for x in symbols}
        self.subscribed |= s
        self.subscribe_calls.append(s)

    async def unsubscribe(self, symbols) -> None:
        s = {x.upper() for x in symbols}
        self.subscribed -= s
        self.unsubscribe_calls.append(s)


class _FakeMarketClient:
    def __init__(self) -> None:
        self.cycles: list[tuple[list[ExchangeSymbol], dict[str, TickerStats]]] = []
        self.index = 0

    def add_cycle(
        self, symbols: list[ExchangeSymbol], tickers: dict[str, TickerStats]
    ) -> None:
        self.cycles.append((symbols, tickers))

    async def fetch_exchange_info(self) -> list[ExchangeSymbol]:
        return self.cycles[self.index][0]

    async def fetch_ticker_24hr(self) -> dict[str, TickerStats]:
        _symbols, tickers = self.cycles[self.index]
        self.index = min(self.index + 1, len(self.cycles) - 1)
        return tickers


def _es(pair: str, status: str = "TRADING") -> ExchangeSymbol:
    base = pair[:-4]
    return ExchangeSymbol(pair=pair, base_asset=base, quote_asset="USDT", status=status)


def _ticker(pair: str, quote_volume: float, change_pct: float = 0.0, count: int = 1000) -> TickerStats:
    return TickerStats(pair=pair, last_price=1.0, quote_volume=quote_volume, price_change_pct=change_pct, count=count)


def _settings(**overrides) -> Settings:
    defaults = {
        "core_symbols": "BTCUSDT,ETHUSDT,SOLUSDT",
        "seed_symbols": "",
        "meme_symbols": "DOGE",
        "tier1_size": 2,
        "promotion_volume_multiplier": 3.0,
        "promotion_volatility_pct": 8.0,
        "demotion_grace_cycles": 2,
        "symbol_refresh_interval_seconds": 10_000.0,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_manager(client: _FakeMarketClient, feed=None, settings: Settings | None = None):
    pool = FakePool()
    pool.fetchrow_return = {"id": 1}
    repo = SymbolRepository(pool)
    manager = SymbolManager(settings or _settings(), repo, feed=feed, market_client=client)
    return manager, pool


# --- discovery / tiering ----------------------------------------------------


async def test_core_meme_and_top_n_tiering() -> None:
    client = _FakeMarketClient()
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("ETHUSDT"), _es("SOLUSDT"), _es("DOGEUSDT"), _es("LINKUSDT"), _es("AVAXUSDT")],
        tickers={
            "BTCUSDT": _ticker("BTCUSDT", 1_000_000),
            "ETHUSDT": _ticker("ETHUSDT", 500_000),
            "SOLUSDT": _ticker("SOLUSDT", 100_000),
            "DOGEUSDT": _ticker("DOGEUSDT", 1_000),  # tiny volume, but curated meme
            "LINKUSDT": _ticker("LINKUSDT", 50_000),  # top-2 by volume among non-core/meme
            "AVAXUSDT": _ticker("AVAXUSDT", 10_000),  # falls to tier 2
        },
    )
    manager, _pool = _make_manager(client, settings=_settings(tier1_size=1))

    await manager.refresh_universe()

    states = manager._states
    assert states["BTC"].tier == 0 and states["BTC"].klass == "core"
    assert states["ETH"].tier == 0 and states["ETH"].klass == "core"
    assert states["SOL"].tier == 0 and states["SOL"].klass == "core"
    assert states["DOGE"].tier == 1 and states["DOGE"].klass == "meme"
    assert states["LINK"].tier == 1 and states["LINK"].klass == "alt"  # tier1_size=1, rank 1 among alts
    assert states["AVAX"].tier == 2 and states["AVAX"].klass == "alt"  # rank 2 among alts, falls outside top-1


async def test_new_listing_is_discovered_as_tier2() -> None:
    client = _FakeMarketClient()
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("NEWUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "NEWUSDT": _ticker("NEWUSDT", 100)},
    )
    manager, _pool = _make_manager(client, settings=_settings(tier1_size=0))

    await manager.refresh_universe()

    assert "NEW" in manager._states
    assert manager._states["NEW"].tier == 2
    assert manager._states["NEW"].status == "trading"


async def test_delisted_symbol_marked_frozen_and_relisted_recovers() -> None:
    client = _FakeMarketClient()
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("GONEUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "GONEUSDT": _ticker("GONEUSDT", 500)},
    )
    # second cycle: GONEUSDT no longer present at all
    client.add_cycle(
        symbols=[_es("BTCUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000)},
    )
    # third cycle: GONEUSDT is back
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("GONEUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "GONEUSDT": _ticker("GONEUSDT", 500)},
    )
    manager, _pool = _make_manager(client, settings=_settings(tier1_size=0))

    await manager.refresh_universe()
    assert manager._states["GONE"].status == "trading"

    changes = await manager.refresh_universe()
    assert manager._states["GONE"].status == "frozen"
    assert any(c.base == "GONE" and c.reason == "delisted" for c in changes)

    await manager.refresh_universe()
    assert manager._states["GONE"].status == "trading"


# --- promotion / demotion ----------------------------------------------------


async def test_volume_spike_promotes_tier2_symbol_to_tier1() -> None:
    client = _FakeMarketClient()
    # cycle 1: AVAX is a quiet tier-2 symbol
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000)},
    )
    # cycle 2: AVAX volume spikes 5x -> should be promoted
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 50_000)},
    )
    manager, _pool = _make_manager(client, settings=_settings(tier1_size=0))

    await manager.refresh_universe()
    assert manager._states["AVAX"].tier == 2

    changes = await manager.refresh_universe()
    assert manager._states["AVAX"].tier == 1
    assert "spike" in manager._states["AVAX"].tags
    assert any(c.base == "AVAX" and c.to_tier == 1 and c.reason == "spike" for c in changes)


async def test_volatility_spike_promotes_tier2_symbol_to_tier1() -> None:
    client = _FakeMarketClient()
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000, change_pct=1.0)},
    )
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000, change_pct=-12.0)},
    )
    manager, _pool = _make_manager(client, settings=_settings(tier1_size=0))

    await manager.refresh_universe()
    assert manager._states["AVAX"].tier == 2

    await manager.refresh_universe()
    assert manager._states["AVAX"].tier == 1
    assert "spike" in manager._states["AVAX"].tags


async def test_spike_promoted_symbol_demotes_after_grace_cycles() -> None:
    client = _FakeMarketClient()
    # cycle 1: quiet
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000)},
    )
    # cycle 2: spike -> promoted
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 50_000)},
    )
    # cycle 3-4: calm again (volume drops back, no volatility)
    for _ in range(2):
        client.add_cycle(
            symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
            tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000)},
        )

    manager, _pool = _make_manager(client, settings=_settings(tier1_size=0, demotion_grace_cycles=2))

    await manager.refresh_universe()  # quiet
    await manager.refresh_universe()  # spike -> tier 1
    assert manager._states["AVAX"].tier == 1

    await manager.refresh_universe()  # calm cycle 1 -> still tier 1 (grace)
    assert manager._states["AVAX"].tier == 1

    changes = await manager.refresh_universe()  # calm cycle 2 -> demoted
    assert manager._states["AVAX"].tier == 2
    assert "spike" not in manager._states["AVAX"].tags
    assert any(c.base == "AVAX" and c.to_tier == 2 and c.reason == "demote_spike" for c in changes)


# --- watchlist ----------------------------------------------------------------


async def test_watchlist_contains_only_tier0_and_tier1_sorted_by_volume() -> None:
    client = _FakeMarketClient()
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("ETHUSDT"), _es("SOLUSDT"), _es("DOGEUSDT"), _es("AVAXUSDT")],
        tickers={
            "BTCUSDT": _ticker("BTCUSDT", 1_000_000),
            "ETHUSDT": _ticker("ETHUSDT", 2_000_000),
            "SOLUSDT": _ticker("SOLUSDT", 100_000),
            "DOGEUSDT": _ticker("DOGEUSDT", 5_000),
            "AVAXUSDT": _ticker("AVAXUSDT", 10_000),
        },
    )
    manager, _pool = _make_manager(client, settings=_settings(tier1_size=0))

    await manager.refresh_universe()
    watchlist = manager.get_watchlist(sort_by="volume")

    bases = [w.base for w in watchlist]
    assert "AVAX" not in bases  # tier 2, excluded

    by_base = {w.base: w for w in watchlist}
    volumes = [by_base[b].quote_volume for b in bases]
    assert volumes == sorted(volumes, reverse=True)
    assert set(bases) == {"BTC", "ETH", "SOL", "DOGE"}


# --- breadth -------------------------------------------------------------------


async def test_handle_snapshot_updates_breadth() -> None:
    client = _FakeMarketClient()
    client.add_cycle(symbols=[], tickers={})
    manager, _pool = _make_manager(client)

    assert manager.get_breadth() is None

    rows = [
        TickerRow(symbol="BTC", price=100.0, change_pct=1.0, quote_volume=1000.0),
        TickerRow(symbol="ETH", price=50.0, change_pct=-1.0, quote_volume=500.0),
    ]
    await manager.handle_snapshot(rows)

    breadth = manager.get_breadth()
    assert breadth is not None
    assert breadth.total == 2
    assert breadth.advancers == 1
    assert breadth.decliners == 1


# --- feed integration ------------------------------------------------------------


async def test_feed_subscribes_watchlist_and_unsubscribes_on_demotion() -> None:
    client = _FakeMarketClient()
    # cycle 1: AVAX is tier1 via volume spike
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000, change_pct=-15.0)},
    )
    # cycle 2: AVAX calm -> still tier1 (grace cycle 1)
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000)},
    )
    # cycle 3: AVAX still calm -> demoted (grace cycle 2)
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000)},
    )
    settings = _settings(tier1_size=0, demotion_grace_cycles=2)
    feed = _FakeFeed()
    # Mirror `build_feed`, which subscribes the feed to `settings.all_symbols`
    # up front (before the manager's first `_sync_feed` reconciliation).
    feed.subscribed |= {s.upper() for s in settings.all_symbols}
    manager, _pool = _make_manager(client, feed=feed, settings=settings)

    await manager.refresh_universe()
    assert "AVAXUSDT" in feed.subscribed
    assert "BTCUSDT" in feed.subscribed

    await manager.refresh_universe()  # grace cycle, still subscribed
    assert "AVAXUSDT" in feed.subscribed

    await manager.refresh_universe()  # demoted -> unsubscribed
    assert "AVAXUSDT" not in feed.subscribed
    assert any("AVAXUSDT" in call for call in feed.unsubscribe_calls)


# --- persistence -----------------------------------------------------------------


async def test_refresh_persists_tier_class_status_tags_and_ranking() -> None:
    client = _FakeMarketClient()
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("LINKUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "LINKUSDT": _ticker("LINKUSDT", 50_000, change_pct=2.5, count=12_345)},
    )
    manager, pool = _make_manager(client, settings=_settings(tier1_size=5))

    await manager.refresh_universe()

    # SymbolRepository.upsert -> INSERT ... ON CONFLICT (base) ... RETURNING id
    upsert_calls = [c for c in pool.fetchrow_calls if "INSERT INTO symbols" in c[0]]
    assert len(upsert_calls) == 2
    btc_call = next(c for c in upsert_calls if c[1][1] == "BTC")
    assert btc_call[1][3] == "core"  # class
    assert btc_call[1][4] == 0  # tier

    # SymbolRepository.update_ranking -> UPDATE symbols SET rank = ..., metrics = ...
    ranking_calls = [c for c in pool.execute_calls if "UPDATE symbols" in c[0]]
    assert len(ranking_calls) == 2
    link_call = next(c for c in ranking_calls if c[1][2] == "LINK")
    rank, metrics_json, _base = link_call[1]
    assert rank == 2  # global rank: behind BTC's higher volume
    metrics = json.loads(metrics_json)
    assert metrics["price_change_pct"] == 2.5
    assert metrics["count"] == 12_345


# --- start/stop loads existing state ------------------------------------------


async def test_start_loads_existing_symbols_and_seeds_spike_set() -> None:
    client = _FakeMarketClient()
    client.add_cycle(
        symbols=[_es("BTCUSDT"), _es("AVAXUSDT")],
        tickers={"BTCUSDT": _ticker("BTCUSDT", 1_000_000), "AVAXUSDT": _ticker("AVAXUSDT", 10_000)},
    )
    pool = FakePool()
    pool.fetchrow_return = {"id": 1}
    pool.fetch_return = [
        {
            "id": 5,
            "symbol": "AVAXUSDT",
            "base": "AVAX",
            "quote": "USDT",
            "class": "alt",
            "tier": 1,
            "status": "trading",
            "tags": ["alt", "spike"],
            "rank": 7,
        }
    ]
    repo = SymbolRepository(pool)
    manager = SymbolManager(_settings(tier1_size=0), repo, market_client=client)

    await manager._load_existing()

    assert "AVAX" in manager._spike_promoted
    assert manager._states["AVAX"].rank == 7
    assert manager._states["AVAX"].tier == 1
    assert manager._states["AVAX"].tags == {"alt", "spike"}
