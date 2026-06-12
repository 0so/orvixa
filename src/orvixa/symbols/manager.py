"""Symbol Manager (Milestone 3).

The automatic-discovery / tiering / ranking / breadth / promotion-demotion
service described in the Phase 2 blueprint. It:

* discovers the live USDT spot universe via :class:`BinanceMarketClient`
  (``/exchangeInfo`` + ``/ticker/24hr``), detecting new listings and
  delistings;
* ranks symbols by :mod:`orvixa.symbols.ranking` and assigns Tier 0
  (configured core), Tier 1 (curated meme set + top-N by volume, plus any
  symbol currently "spiking"), and Tier 2 (everything else);
* promotes Tier-2 symbols whose 24h volume/activity spikes or whose 24h
  change exceeds the volatility threshold, and demotes them back after
  ``demotion_grace_cycles`` calm refresh cycles;
* persists tier/class/status/tags/rank/metrics via
  :class:`~orvixa.db.repository.SymbolRepository`;
* keeps an injected :class:`~orvixa.feeds.base.MarketFeed` subscribed to
  exactly the Tier 0 + Tier 1 watchlist (no restart needed);
* feeds whole-market ``on_market_snapshot`` data into
  :class:`~orvixa.symbols.breadth.BreadthEngine` for live breadth metrics.

Refresh cycles run on a periodic scheduler (:meth:`start`/:meth:`stop`), and
``refresh_universe`` can also be called directly — both paths are exercised
by the test suite with a fake market client and feed (no live Binance).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import Settings
from ..db.models import SymbolRow, TierChangeRow
from ..db.repository import SymbolRepository, TierChangeRepository
from ..feeds.base import MarketFeed, TickerRow
from ..feeds.normalize import normalize_symbol
from .breadth import BreadthEngine
from .client import BinanceMarketClient
from .models import BreadthSnapshot, RankedSymbol, TierChange
from .ranking import rank_by_score
from .watchlist import build_watchlist, sort_watchlist

logger = logging.getLogger("orvixa.symbols.manager")

# exchangeInfo statuses treated as "currently tradable".
_ACTIVE_STATUSES = frozenset({"TRADING"})

TIER_CORE = 0
TIER_WATCH = 1
TIER_REST = 2


@dataclass(slots=True)
class _SymbolState:
    base: str
    pair: str
    tier: int
    klass: str  # "core" | "alt" | "meme"
    status: str  # "trading" | "frozen"
    tags: set[str] = field(default_factory=set)
    rank: int | None = None
    quote_volume: float = 0.0
    price_change_pct: float = 0.0
    last_price: float = 0.0
    count: int = 0
    prev_quote_volume: float | None = None
    prev_count: int | None = None
    calm_cycles: int = 0


class SymbolManager:
    """Discovers, ranks, tiers, and watches the USDT spot universe."""

    def __init__(
        self,
        settings: Settings,
        symbol_repo: SymbolRepository,
        feed: MarketFeed | None = None,
        market_client: BinanceMarketClient | None = None,
        breadth_engine: BreadthEngine | None = None,
        tier_change_repo: TierChangeRepository | None = None,
    ) -> None:
        self._settings = settings
        self._symbol_repo = symbol_repo
        self._tier_change_repo = tier_change_repo
        self._feed = feed
        self._market_client = market_client or BinanceMarketClient(rest_base=settings.binance_rest_base)
        self._breadth = breadth_engine or BreadthEngine(trend_window=settings.breadth_trend_window)

        self._core: set[str] = {normalize_symbol(s) for s in settings.core_symbols}
        self._meme: set[str] = {normalize_symbol(s) for s in settings.meme_symbols}

        self._states: dict[str, _SymbolState] = {}
        self._spike_promoted: set[str] = set()
        # The feed (built via `build_feed`) starts subscribed to
        # `settings.all_symbols` — seed this set so the first `_sync_feed`
        # call can correctly unsubscribe any of those pairs that aren't part
        # of the Tier 0/1 watchlist.
        self._subscribed: set[str] = {s.upper() for s in settings.all_symbols}
        self._latest_breadth: BreadthSnapshot | None = None

        # `_loop` only ever calls `refresh_universe` sequentially, but guard
        # against an overlapping call (e.g. a future manual "refresh now"
        # trigger) mutating `_states` mid-iteration.
        self._refresh_lock = asyncio.Lock()

        self._task: asyncio.Task | None = None
        self._running = False

        # Observable state for tests/ops.
        self.refresh_count = 0
        self.last_tier_changes: list[TierChange] = []

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._feed is not None:
            self._feed.on_market_snapshot(self.handle_snapshot)
        await self._load_existing()
        await self.refresh_universe()
        self._task = asyncio.create_task(self._loop(), name="symbol-manager-loop")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self._settings.symbol_refresh_interval_seconds)
                if not self._running:
                    break
                await self.refresh_universe()
        except asyncio.CancelledError:
            raise

    # -- state seeding ------------------------------------------------------
    async def _load_existing(self) -> None:
        """Carry tier/class/status/tags/rank forward from a prior run."""
        rows = await self._symbol_repo.list_all()
        for row in rows:
            base = row["base"]
            tags = set(row["tags"] or [])
            self._states[base] = _SymbolState(
                base=base,
                pair=row["symbol"],
                tier=row["tier"],
                klass=row["class"],
                status=row["status"],
                tags=tags,
                rank=row["rank"] if "rank" in row.keys() else None,
            )
            if "spike" in tags:
                self._spike_promoted.add(base)

    # -- breadth (real-time, from the feed's snapshot stream) ---------------
    async def handle_snapshot(self, rows: list[TickerRow]) -> None:
        self._latest_breadth = self._breadth.update(rows)

    def get_breadth(self) -> BreadthSnapshot | None:
        return self._latest_breadth

    # -- watchlist ------------------------------------------------------------
    def get_watchlist(self, sort_by: str = "volume") -> list[RankedSymbol]:
        ranked = [
            RankedSymbol(
                base=s.base,
                pair=s.pair,
                tier=s.tier,
                klass=s.klass,
                status=s.status,
                rank=s.rank,
                quote_volume=s.quote_volume,
                price_change_pct=s.price_change_pct,
                last_price=s.last_price,
                tags=sorted(s.tags),
            )
            for s in self._states.values()
            if s.status == "trading"
        ]
        return sort_watchlist(build_watchlist(ranked), sort_by=sort_by)

    # -- the refresh cycle ----------------------------------------------------
    async def refresh_universe(self) -> list[TierChange]:
        """Discover, rank, re-tier, persist, and re-subscribe the feed.

        Returns the list of tier transitions made this cycle. Serialized via
        `_refresh_lock` — `_states` is mutated in place (incl. inserting new
        keys on discovery) while iterated for persistence, which would raise
        if two refreshes ever overlapped.
        """
        async with self._refresh_lock:
            exchange_symbols = await self._market_client.fetch_exchange_info()
            tickers = await self._market_client.fetch_ticker_24hr()
            active = {es.pair: es for es in exchange_symbols if es.status in _ACTIVE_STATUSES}

            rank_of, top_n = self._rank_universe(active, tickers)
            changes: list[TierChange] = []

            self._mark_delistings(active, changes)
            self._sync_listings(active, tickers, rank_of)
            self._assign_tiers(top_n, changes)

            for state in self._states.values():
                await self._persist(state)

            await self._persist_tier_changes(changes)

            self.refresh_count += 1
            self.last_tier_changes = changes

            if self._feed is not None:
                await self._sync_feed()

            return changes

    def _rank_universe(self, active, tickers):
        # Global rank (stored on each symbol as `rank`/`metrics.rank`) is over
        # the whole active universe, core included.
        active_stats = [tickers[pair] for pair in active if pair in tickers]
        ranked = rank_by_score(active_stats)
        rank_of = {stats.pair: i + 1 for i, (stats, _score) in enumerate(ranked)}

        # Tier-1 "top volume symbols" are the top `tier1_size` *alts* — Tier 0
        # (core) and the curated meme set already get Tier 1+ regardless of
        # rank, so they don't consume a top-N slot.
        alt_stats = [
            tickers[pair]
            for pair, es in active.items()
            if pair in tickers and es.base not in self._core and es.base not in self._meme
        ]
        alt_ranked = rank_by_score(alt_stats)
        top_n = {stats.pair for stats, _score in alt_ranked[: self._settings.tier1_size]}

        return rank_of, top_n

    def _mark_delistings(self, active, changes: list[TierChange]) -> None:
        for base, state in self._states.items():
            if state.pair not in active and state.status != "frozen":
                state.status = "frozen"
                changes.append(
                    TierChange(base, state.pair, state.tier, state.tier, "delisted")
                )
                logger.info("symbol delisted", extra={"base": base, "pair": state.pair})

    def _sync_listings(self, active, tickers, rank_of) -> None:
        for pair, es in active.items():
            base = es.base
            state = self._states.get(base)
            if state is None:
                state = _SymbolState(base=base, pair=pair, tier=TIER_REST, klass="alt", status="trading")
                self._states[base] = state
                logger.info("symbol discovered", extra={"base": base, "pair": pair})
            elif state.status == "frozen":
                state.status = "trading"
                logger.info("symbol relisted", extra={"base": base, "pair": pair})

            state.pair = pair
            stats = tickers.get(pair)
            if stats is not None:
                state.prev_quote_volume = state.quote_volume
                state.prev_count = state.count
                state.quote_volume = stats.quote_volume
                state.price_change_pct = stats.price_change_pct
                state.last_price = stats.last_price
                state.count = stats.count
            state.rank = rank_of.get(pair)

    def _assign_tiers(self, top_n: set[str], changes: list[TierChange]) -> None:
        for base, state in self._states.items():
            if state.status == "frozen":
                continue

            old_tier, old_klass = state.tier, state.klass
            base_tier, klass = self._base_tier(base, state, top_n)
            tier, klass = self._apply_spike_logic(base, state, base_tier, klass)

            tags = {klass}
            if base in self._spike_promoted and base_tier == TIER_REST:
                tags.add("spike")
            state.tags = tags

            if tier != old_tier or klass != old_klass:
                if tier == TIER_WATCH and base_tier == TIER_REST:
                    reason = "spike"
                elif old_tier == TIER_WATCH and tier == TIER_REST:
                    reason = "demote_spike"
                else:
                    reason = "ranking"
                changes.append(TierChange(base, state.pair, old_tier, tier, reason))
                logger.info(
                    "tier change",
                    extra={"base": base, "from_tier": old_tier, "to_tier": tier, "reason": reason},
                )

            state.tier, state.klass = tier, klass

    def _base_tier(self, base: str, state: _SymbolState, top_n: set[str]) -> tuple[int, str]:
        if base in self._core:
            return TIER_CORE, "core"
        if base in self._meme:
            return TIER_WATCH, "meme"
        if state.pair in top_n:
            return TIER_WATCH, "alt"
        return TIER_REST, "alt"

    def _apply_spike_logic(
        self, base: str, state: _SymbolState, base_tier: int, klass: str
    ) -> tuple[int, str]:
        if base_tier != TIER_REST:
            self._spike_promoted.discard(base)
            return base_tier, klass

        if self._is_spiking(state):
            if base not in self._spike_promoted:
                logger.info("symbol spike-promoted", extra={"base": base, "pair": state.pair})
            self._spike_promoted.add(base)
            state.calm_cycles = 0
            return TIER_WATCH, "alt"

        if base in self._spike_promoted:
            state.calm_cycles += 1
            if state.calm_cycles >= self._settings.demotion_grace_cycles:
                self._spike_promoted.discard(base)
                state.calm_cycles = 0
                return TIER_REST, klass
            return TIER_WATCH, "alt"

        return TIER_REST, klass

    def _is_spiking(self, state: _SymbolState) -> bool:
        mult = self._settings.promotion_volume_multiplier
        if (
            state.prev_quote_volume is not None
            and state.prev_quote_volume > 0
            and state.quote_volume >= state.prev_quote_volume * mult
        ):
            return True
        if (
            state.prev_count is not None
            and state.prev_count > 0
            and state.count >= state.prev_count * mult
        ):
            return True
        return abs(state.price_change_pct) >= self._settings.promotion_volatility_pct

    # -- persistence ----------------------------------------------------------
    async def _persist(self, state: _SymbolState) -> None:
        row = SymbolRow(
            symbol=state.pair,
            base=state.base,
            klass=state.klass,
            tier=state.tier,
            status=state.status,
            tags=sorted(state.tags),
        )
        await self._symbol_repo.upsert(row)
        await self._symbol_repo.update_ranking(
            state.base,
            rank=state.rank,
            metrics={
                "quote_volume": state.quote_volume,
                "price_change_pct": state.price_change_pct,
                "last_price": state.last_price,
                "count": state.count,
            },
        )

    async def _persist_tier_changes(self, changes: list[TierChange]) -> None:
        """Log every tier transition with a reliable timestamp (M3 30-day evaluation)."""
        if self._tier_change_repo is None or not changes:
            return
        ts = datetime.now(tz=UTC)
        for change in changes:
            symbol_id = await self._symbol_repo.get_id(change.base)
            if symbol_id is None:
                logger.warning(
                    "tier change for unresolved symbol; not persisted", extra={"base": change.base}
                )
                continue
            await self._tier_change_repo.insert(
                TierChangeRow(
                    symbol_id=symbol_id,
                    ts=ts,
                    from_tier=change.from_tier,
                    to_tier=change.to_tier,
                    reason=change.reason,
                )
            )

    # -- feed integration -------------------------------------------------------
    async def _sync_feed(self) -> None:
        assert self._feed is not None
        watchlist_pairs = {
            state.pair
            for state in self._states.values()
            if state.tier in (TIER_CORE, TIER_WATCH) and state.status == "trading"
        }

        new = watchlist_pairs - self._subscribed
        dropped = self._subscribed - watchlist_pairs

        if new:
            await self._feed.subscribe(new)
            logger.info("watchlist subscribe", extra={"pairs": sorted(new)})
        if dropped:
            await self._feed.unsubscribe(dropped)
            logger.info("watchlist unsubscribe", extra={"pairs": sorted(dropped)})

        self._subscribed = watchlist_pairs
