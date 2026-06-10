"""Symbol Manager (Milestone 3) — discovery, ranking, breadth, watchlist.

:class:`~orvixa.symbols.manager.SymbolManager` is the orchestration seam:
it discovers the USDT spot universe via :class:`BinanceMarketClient`, ranks
it (:mod:`ranking`), assigns Tier 0/1/2 with promotion/demotion
(:mod:`manager`), tracks market breadth (:mod:`breadth`), builds the dynamic
watchlist (:mod:`watchlist`), persists symbol metadata via
:class:`~orvixa.db.repository.SymbolRepository`, and keeps an injected
:class:`~orvixa.feeds.base.MarketFeed` subscribed to the watchlist.
"""

from __future__ import annotations

from .breadth import BreadthEngine
from .client import BinanceMarketClient
from .manager import SymbolManager
from .models import BreadthSnapshot, ExchangeSymbol, RankedSymbol, TickerStats, TierChange
from .ranking import compute_score, rank_by_score
from .watchlist import build_watchlist, sort_watchlist

__all__ = [
    "BreadthEngine",
    "BinanceMarketClient",
    "SymbolManager",
    "BreadthSnapshot",
    "ExchangeSymbol",
    "RankedSymbol",
    "TickerStats",
    "TierChange",
    "compute_score",
    "rank_by_score",
    "build_watchlist",
    "sort_watchlist",
]
