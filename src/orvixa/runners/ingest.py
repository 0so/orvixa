"""``ingest`` — the Milestone-2 persistence runner.

Boots the configured feed (``FEED=sim|binance``, unchanged from M1), seeds the
``symbols`` registry, and wires every closed candle through a
:class:`~orvixa.persistence.batch_writer.BatchWriter` into the ``candles``
hypertable — batched every ``candle_batch_interval_seconds`` /
``candle_batch_max_size``, whichever comes first.

Run it with ``make ingest`` (host, needs Postgres) or as the ``app`` service
in ``docker-compose.dev.yml``. ``orvixa-feedcheck`` (M1, no DB) is untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time

from ..config import get_settings
from ..db import CandleRepository, SymbolRepository, create_pool
from ..factory import build_feed
from ..feeds.base import Candle
from ..logging import get_logger, setup_logging
from ..persistence import BatchWriter, CandleSink, candle_repository_sink, seed_symbols


class _Stats:
    """Tiny rolling counter so the runner can show signs of life."""

    def __init__(self) -> None:
        self.candles = 0
        self.flushes = 0
        self.started = time.time()


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger("orvixa.ingest")
    stats = _Stats()

    log.info(
        "ingest starting",
        extra={
            "feed": settings.feed,
            "symbols": settings.all_symbols,
            "interval": settings.kline_interval,
        },
    )

    pool = await create_pool(settings)
    try:
        symbol_repo = SymbolRepository(pool)
        candle_repo = CandleRepository(pool)

        symbol_ids = await seed_symbols(symbol_repo, settings)
        log.info("symbols seeded", extra={"count": len(symbol_ids)})

        batch_writer: BatchWriter = BatchWriter(
            sink=candle_repository_sink(candle_repo),
            max_size=settings.candle_batch_max_size,
            interval_seconds=settings.candle_batch_interval_seconds,
            name="candle-batch-writer",
        )
        candle_sink = CandleSink(
            symbol_repo,
            batch_writer,
            interval=settings.kline_interval,
            symbol_ids=symbol_ids,
        )

        feed = build_feed(settings)

        async def on_candle(candle: Candle) -> None:
            if candle.closed:
                stats.candles += 1
            await candle_sink.handle_candle(candle)

        feed.on_candle_close(on_candle)

        stop_event = asyncio.Event()

        def _request_stop() -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
                loop.add_signal_handler(sig, _request_stop)

        await batch_writer.start()
        await feed.start()
        log.info("ingest running — Ctrl-C to stop")
        try:
            await stop_event.wait()
        finally:
            await feed.stop()
            await batch_writer.stop()
            stats.flushes = batch_writer.flush_count
            elapsed = time.time() - stats.started
            log.info(
                "ingest stopped",
                extra={
                    "candles": stats.candles,
                    "flushes": stats.flushes,
                    "uptime_s": round(elapsed, 1),
                },
            )
    finally:
        await pool.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
