"""``analytics`` — the Milestone-4 deterministic analytics runner.

Boots the configured feed (unchanged ``FEED=sim|binance`` switch from M1),
starts :class:`~orvixa.analytics.engine.AnalyticsEngine` against every closed
candle and whole-market snapshot, and periodically logs progress + the latest
regime/health. Indicators/signals/events/market_memory are persisted via the
M4 repositories. ``feedcheck``/``ingest``/``symbols`` (M1-M3) are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from ..analytics.engine import AnalyticsEngine, indicator_repository_sink
from ..config import get_settings
from ..db import (
    IndicatorRepository,
    MarketEventRepository,
    MarketMemoryRepository,
    SignalRepository,
    SymbolRepository,
    create_pool,
)
from ..db.models import IndicatorRow
from ..factory import build_feed
from ..logging import get_logger, setup_logging
from ..persistence import BatchWriter, seed_symbols

_LOG_INTERVAL_SECONDS = 60.0


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger("orvixa.analytics")

    log.info(
        "analytics engine starting",
        extra={
            "feed": settings.feed,
            "ema_fast": settings.ema_fast_period,
            "ema_slow": settings.ema_slow_period,
            "regime_refresh_interval_s": settings.regime_refresh_interval_seconds,
        },
    )

    pool = await create_pool(settings)
    try:
        symbol_repo = SymbolRepository(pool)
        indicator_repo = IndicatorRepository(pool)
        signal_repo = SignalRepository(pool)
        event_repo = MarketEventRepository(pool)
        memory_repo = MarketMemoryRepository(pool)

        symbol_ids = await seed_symbols(symbol_repo, settings)
        log.info("symbols seeded", extra={"count": len(symbol_ids)})

        indicator_writer: BatchWriter[IndicatorRow] = BatchWriter(
            sink=indicator_repository_sink(indicator_repo),
            max_size=settings.indicator_batch_max_size,
            interval_seconds=settings.indicator_batch_interval_seconds,
            name="indicator-batch-writer",
        )

        engine = AnalyticsEngine(
            settings,
            symbol_repo,
            indicator_writer,
            signal_repo,
            event_repo,
            memory_repo,
            symbol_ids=symbol_ids,
        )

        feed = build_feed(settings)
        feed.on_candle_close(engine.handle_candle)
        feed.on_market_snapshot(engine.handle_snapshot)

        stop_event = asyncio.Event()

        def _request_stop() -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # Windows lacks add_signal_handler
                loop.add_signal_handler(sig, _request_stop)

        await indicator_writer.start()
        await feed.start()
        await engine.start()
        log.info("analytics engine running — Ctrl-C to stop")

        try:
            while True:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_LOG_INTERVAL_SECONDS)
                    break
                except TimeoutError:
                    pass

                breadth = engine.get_breadth()
                log.info(
                    "analytics progress",
                    extra={
                        "candles_processed": engine.candles_processed,
                        "signals_emitted": engine.signals_emitted,
                        "events_emitted": engine.events_emitted,
                        "regime_refresh_count": engine.regime_refresh_count,
                        "ad_ratio": round(breadth.ad_ratio, 2) if breadth else None,
                    },
                )
        finally:
            await engine.stop()
            await feed.stop()
            await indicator_writer.stop()
            log.info(
                "analytics engine stopped",
                extra={
                    "candles_processed": engine.candles_processed,
                    "signals_emitted": engine.signals_emitted,
                    "events_emitted": engine.events_emitted,
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
