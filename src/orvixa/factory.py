"""Feed factory — builds the configured :class:`MarketFeed`.

This is the single place the ``FEED=sim|binance`` switch is resolved. Every
consumer (the runner now, the engine in later milestones) calls
``build_feed(settings)`` and never imports a concrete source directly.
"""

from __future__ import annotations

from .config import Settings
from .feeds.base import MarketFeed
from .feeds.binance import BinanceFeed
from .feeds.sim import SimFeed


def build_feed(settings: Settings) -> MarketFeed:
    if settings.feed == "binance":
        return BinanceFeed(
            symbols=settings.all_symbols,
            ws_base=settings.binance_ws_base,
            rest_base=settings.binance_rest_base,
            interval=settings.kline_interval,
            backfill_limit=settings.backfill_limit,
        )
    return SimFeed(
        symbols=settings.all_symbols,
        candle_seconds=settings.sim_candle_seconds,
    )
