"""Generic, size/time-triggered async batch writer.

:class:`BatchWriter` decouples producers (e.g. a feed's ``on_candle_close``
callback) from a slow or bursty sink (e.g. a Postgres ``executemany``): items
are buffered and flushed either when ``max_size`` is reached or every
``interval_seconds``, whichever comes first — matching the M2 done-criteria
of "live candles persist in 1-5s batches".

The sink is injected as a plain async callable, so tests can pass a fake that
records batches with no database involved (the same pattern M1 used for the
WebSocket connector).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

logger = logging.getLogger("orvixa.persistence.batch_writer")

T = TypeVar("T")

Sink = Callable[[list[T]], Awaitable[object]]


class BatchWriter(Generic[T]):
    """Buffer items and flush them to ``sink`` by size or by time."""

    def __init__(
        self,
        sink: Sink,
        max_size: int = 200,
        interval_seconds: float = 2.0,
        name: str = "batch-writer",
    ) -> None:
        self._sink = sink
        self._max_size = max_size
        self._interval = interval_seconds
        self._name = name

        self._buffer: list[T] = []
        self._lock = asyncio.Lock()
        self._flush_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._running = False

        # Observable state for tests / ops.
        self.flush_count = 0
        self.error_count = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name=self._name)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._flush_event.set()
        if self._task:
            await self._task
            self._task = None
        # Final drain of anything added after the loop's last flush.
        await self._flush()

    async def add(self, item: T) -> None:
        async with self._lock:
            self._buffer.append(item)
            full = len(self._buffer) >= self._max_size
        if full:
            self._flush_event.set()

    async def add_many(self, items: list[T]) -> None:
        if not items:
            return
        async with self._lock:
            self._buffer.extend(items)
            full = len(self._buffer) >= self._max_size
        if full:
            self._flush_event.set()

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(self._flush_event.wait(), timeout=self._interval)
            except TimeoutError:
                pass
            self._flush_event.clear()
            await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer, []
        try:
            await self._sink(batch)
            self.flush_count += 1
        except Exception:  # noqa: BLE001
            self.error_count += 1
            logger.exception("%s: sink raised on batch of %d", self._name, len(batch))
