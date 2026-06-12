"""Daemon supervisor — run a runner's ``run()`` coroutine forever.

Phase 2 turns the batch/one-shot CLI runners into persistent background
services without touching their logic. The existing ``ingest.run`` and
``analytics.run`` coroutines already block until SIGINT/SIGTERM; this
supervisor wraps them in the requested

    while True:
        run()
        sleep(interval)

loop so that if a runner returns (clean shutdown of its feed) or crashes, the
service waits ``ORVIXA_DAEMON_INTERVAL_SECONDS`` and restarts it instead of
exiting the container. Logging and metrics inside each ``run()`` are left
exactly as-is. SIGTERM/SIGINT break the loop for a clean container stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable

from ..config import get_settings
from ..logging import get_logger, setup_logging


async def supervise(name: str, run: Callable[[], Awaitable[None]]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger(f"orvixa.daemon.{name}")
    interval = settings.daemon_interval_seconds

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    log.info("daemon starting", extra={"runner": name, "interval_s": interval})
    while not stop_event.is_set():
        try:
            await run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the service alive across runner crashes
            log.exception("runner crashed; will restart", extra={"runner": name})
        else:
            log.info("runner exited; will restart", extra={"runner": name})

        if stop_event.is_set():
            break
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=interval)

    log.info("daemon stopped", extra={"runner": name})


def run_ingest_daemon() -> None:
    from .ingest import run as ingest_run

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(supervise("ingest", ingest_run))


def run_analytics_daemon() -> None:
    from .analytics import run as analytics_run

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(supervise("analytics", analytics_run))


def run_symbols_daemon() -> None:
    from .symbols import run as symbols_run

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(supervise("symbols", symbols_run))


def main() -> None:
    """``python -m orvixa.runners.daemon {ingest|analytics|symbols}``."""
    import sys

    choice = sys.argv[1] if len(sys.argv) > 1 else "analytics"
    if choice == "ingest":
        run_ingest_daemon()
    elif choice == "analytics":
        run_analytics_daemon()
    elif choice == "symbols":
        run_symbols_daemon()
    else:
        raise SystemExit(
            f"usage: python -m orvixa.runners.daemon {{ingest|analytics|symbols}} (got {choice!r})"
        )


if __name__ == "__main__":
    main()
