"""Tests for the M5 CSV historical-candle loader -- no database required."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from fake_pool import FakePool
from orvixa.backfill import load_candles_csv

_HEADER = [
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "taker_buy_volume",
]


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_HEADER)
        writer.writerows(rows)


def _pool(symbol_id: int | None) -> FakePool:
    pool = FakePool()
    pool.fetchval_return = symbol_id
    return pool


async def test_load_candles_csv_iso_timestamps(tmp_path: Path) -> None:
    csv_path = tmp_path / "BTC.csv"
    _write_csv(
        csv_path,
        [
            ["2026-05-01T00:00:00+00:00", "100", "101", "99", "100.5", "10", "1005", "5", "6"],
            ["2026-05-01T00:01:00+00:00", "100.5", "102", "100", "101", "12", "1212", "6", "7"],
        ],
    )

    pool = _pool(symbol_id=1)
    written = await load_candles_csv(pool, "BTC", csv_path)

    assert written == 2
    assert len(pool.executemany_calls) == 1
    _query, args = pool.executemany_calls[0]
    assert len(args) == 2

    first = args[0]
    assert first[0] == 1  # symbol_id
    assert first[2] == "1m"  # interval
    assert first[3] == 100.0  # open
    assert first[6] == 100.5  # close
    assert first[9] == 5  # trades


async def test_load_candles_csv_epoch_ms_timestamps(tmp_path: Path) -> None:
    csv_path = tmp_path / "ETH.csv"
    _write_csv(
        csv_path,
        [["1780000000000", "1", "1.1", "0.9", "1.05", "100", "105", "3", "1"]],
    )

    pool = _pool(symbol_id=2)
    written = await load_candles_csv(pool, "ETH", csv_path)

    assert written == 1
    _query, args = pool.executemany_calls[0]
    assert args[0][1].timestamp() == 1_780_000_000_000 / 1000


async def test_load_candles_csv_batches(tmp_path: Path) -> None:
    csv_path = tmp_path / "SOL.csv"
    rows = [
        [str(1_780_000_000_000 + i * 60_000), "1", "1", "1", "1", "1", "1", "1", "1"]
        for i in range(5)
    ]
    _write_csv(csv_path, rows)

    pool = _pool(symbol_id=3)
    written = await load_candles_csv(pool, "SOL", csv_path, batch_size=2)

    assert written == 5
    assert len(pool.executemany_calls) == 3  # batches of 2, 2, 1
    sizes = [len(args) for _query, args in pool.executemany_calls]
    assert sizes == [2, 2, 1]


async def test_load_candles_csv_unknown_symbol_raises(tmp_path: Path) -> None:
    csv_path = tmp_path / "NOPE.csv"
    _write_csv(csv_path, [["2026-05-01T00:00:00+00:00", "1", "1", "1", "1", "1", "1", "1", "1"]])

    pool = _pool(symbol_id=None)
    with pytest.raises(ValueError, match="unknown symbol"):
        await load_candles_csv(pool, "NOPE", csv_path)
