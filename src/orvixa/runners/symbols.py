"""``symbols`` — the Milestone-3 Symbol Manager runner.

Boots the configured feed (unchanged ``FEED=sim|binance`` switch from M1),
starts :class:`~orvixa.symbols.manager.SymbolManager` (automatic discovery,
tiering, ranking, breadth, promotion/demotion), and periodically logs the
watchlist + breadth. Symbol metadata (tier/class/status/tags/rank/metrics) is
persisted to the ``symbols`` table. ``feedcheck`` (M1) and ``ingest`` (M2)
are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from ..config import get_settings
from ..db import SymbolRepository, create_pool
from ..factory import build_feed
from ..logging import get_logger, setup_logging
from ..symbols.manager import SymbolManager

_LOG_INTERVAL_SECONDS = 60.0


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger("orvixa.symbols")

    log.info(
        "symbol manager starting",
        extra={
            "feed": settings.feed,
            "tier1_size": settings.tier1_size,
            "refresh_interval_s": settings.symbol_refresh_interval_seconds,
        },
    )

    pool = await create_pool(settings)
    try:
        symbol_repo = SymbolRepository(pool)
        feed = build_feed(settings)
        manager = SymbolManager(settings, symbol_repo, feed=feed)

        stop_event = asyncio.Event()

        def _request_stop() -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
                loop.add_signal_handler(sig, _request_stop)

        await feed.start()
        await manager.start()
        log.info("symbol manager running — Ctrl-C to stop")

        try:
            while True:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_LOG_INTERVAL_SECONDS)
                    break
                except TimeoutError:
                    pass

                watchlist = manager.get_watchlist()
                log.info(
                    "watchlist",
                    extra={"size": len(watchlist), "symbols": [w.base for w in watchlist]},
                )
                breadth = manager.get_breadth()
                if breadth is not None:
                    log.info(
                        "breadth",
                        extra={
                            "advancers": breadth.advancers,
                            "decliners": breadth.decliners,
                            "ad_ratio": round(breadth.ad_ratio, 2),
                            "pct_above_trend": round(breadth.pct_above_trend, 1),
                            "new_highs": breadth.new_highs,
                            "new_lows": breadth.new_lows,
                        },
                    )
        finally:
            await manager.stop()
            await feed.stop()
            log.info("symbol manager stopped", extra={"refresh_count": manager.refresh_count})
    finally:
        await pool.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
