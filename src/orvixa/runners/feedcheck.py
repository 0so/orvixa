"""``feedcheck`` — the Milestone-1 acceptance runner.

Boots the configured feed, subscribes consumers to the two event hooks, and
prints:

* one structured line per **closed** 1-minute candle, and
* a periodic **breadth** summary derived from whole-market snapshots
  (advancers / decliners / net), proving the snapshot path works too.

Run it with ``make feedcheck`` (host) or ``make dev`` (Docker). Ctrl-C exits
cleanly. This is the observable proof that M1 is done — no DB, no HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time

from ..config import get_settings
from ..factory import build_feed
from ..feeds.base import Candle, TickerRow
from ..logging import get_logger, setup_logging


class _Stats:
    """Tiny rolling counter so the runner can show signs of life."""

    def __init__(self) -> None:
        self.candles = 0
        self.snapshots = 0
        self.started = time.time()


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger("orvixa.feedcheck")
    stats = _Stats()

    log.info(
        "feedcheck starting",
        extra={
            "feed": settings.feed,
            "symbols": settings.all_symbols,
            "interval": settings.kline_interval,
        },
    )

    feed = build_feed(settings)

    async def on_candle(candle: Candle) -> None:
        if not candle.closed:
            return  # M1 only logs finalized bars
        stats.candles += 1
        log.info(
            "candle",
            extra={
                "symbol": candle.symbol,
                "o": candle.open,
                "h": candle.high,
                "l": candle.low,
                "c": candle.close,
                "qv": round(candle.quote_volume, 2),
                "dir": "up" if candle.is_bullish else "down",
            },
        )

    async def on_snapshot(rows: list[TickerRow]) -> None:
        stats.snapshots += 1
        if not rows:
            return
        # Emit a breadth line at most ~once every 10 snapshots to avoid noise.
        if stats.snapshots % 10 != 0:
            return
        advancers = sum(1 for r in rows if r.change_pct > 0)
        decliners = sum(1 for r in rows if r.change_pct < 0)
        leader = max(rows, key=lambda r: r.change_pct)
        laggard = min(rows, key=lambda r: r.change_pct)
        log.info(
            "breadth",
            extra={
                "tracked": len(rows),
                "advancers": advancers,
                "decliners": decliners,
                "net": advancers - decliners,
                "leader": f"{leader.symbol} {leader.change_pct:+.2f}%",
                "laggard": f"{laggard.symbol} {laggard.change_pct:+.2f}%",
                "candles_seen": stats.candles,
            },
        )

    feed.on_candle_close(on_candle)
    feed.on_market_snapshot(on_snapshot)

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
            loop.add_signal_handler(sig, _request_stop)

    await feed.start()
    log.info("feedcheck running — Ctrl-C to stop")
    try:
        await stop_event.wait()
    finally:
        await feed.stop()
        elapsed = time.time() - stats.started
        log.info(
            "feedcheck stopped",
            extra={
                "candles": stats.candles,
                "snapshots": stats.snapshots,
                "uptime_s": round(elapsed, 1),
            },
        )


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
