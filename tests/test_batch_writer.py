"""``BatchWriter``: size/time-triggered flush, graceful stop, error isolation."""

from __future__ import annotations

import asyncio

import pytest

from orvixa.persistence.batch_writer import BatchWriter


class _Sink:
    def __init__(self, fail_times: int = 0) -> None:
        self.batches: list[list[int]] = []
        self._fail_times = fail_times

    async def __call__(self, batch: list[int]) -> None:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("sink boom")
        self.batches.append(batch)


async def test_flush_on_max_size() -> None:
    sink = _Sink()
    writer: BatchWriter[int] = BatchWriter(sink, max_size=3, interval_seconds=10.0)
    await writer.start()
    try:
        await writer.add(1)
        await writer.add(2)
        await writer.add(3)  # hits max_size -> triggers flush
        # give the background task a tick to act on the flush event
        for _ in range(50):
            if sink.batches:
                break
            await asyncio.sleep(0.01)
    finally:
        await writer.stop()

    assert sink.batches == [[1, 2, 3]]


async def test_flush_on_interval() -> None:
    sink = _Sink()
    writer: BatchWriter[int] = BatchWriter(sink, max_size=200, interval_seconds=0.05)
    await writer.start()
    try:
        await writer.add(42)
        for _ in range(50):
            if sink.batches:
                break
            await asyncio.sleep(0.01)
    finally:
        await writer.stop()

    assert sink.batches == [[42]]
    assert writer.flush_count == 1


async def test_stop_drains_remaining_items() -> None:
    sink = _Sink()
    writer: BatchWriter[int] = BatchWriter(sink, max_size=200, interval_seconds=10.0)
    await writer.start()
    await writer.add(1)
    await writer.add(2)
    await writer.stop()

    assert sink.batches == [[1, 2]]


async def test_add_many_respects_max_size_trigger() -> None:
    sink = _Sink()
    writer: BatchWriter[int] = BatchWriter(sink, max_size=2, interval_seconds=10.0)
    await writer.start()
    try:
        await writer.add_many([1, 2, 3])
        for _ in range(50):
            if sink.batches:
                break
            await asyncio.sleep(0.01)
    finally:
        await writer.stop()

    assert sink.batches == [[1, 2, 3]]


async def test_sink_error_is_isolated_and_counted() -> None:
    sink = _Sink(fail_times=1)
    writer: BatchWriter[int] = BatchWriter(sink, max_size=1, interval_seconds=10.0)
    await writer.start()
    try:
        await writer.add(1)  # flush raises, recorded as error
        for _ in range(50):
            if writer.error_count:
                break
            await asyncio.sleep(0.01)
        await writer.add(2)  # next flush succeeds
        for _ in range(50):
            if sink.batches:
                break
            await asyncio.sleep(0.01)
    finally:
        await writer.stop()

    assert writer.error_count == 1
    assert sink.batches == [[2]]


async def test_double_start_and_stop_are_idempotent() -> None:
    sink = _Sink()
    writer: BatchWriter[int] = BatchWriter(sink, max_size=10, interval_seconds=10.0)
    await writer.start()
    await writer.start()
    await writer.stop()
    await writer.stop()


if __name__ == "__main__":
    pytest.main([__file__])
