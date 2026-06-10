"""CSV-based historical candle loader -- the M5 data-foundation layer.

Reads 1m OHLCV rows from a CSV file and writes them into the existing
``candles`` table via :class:`~orvixa.db.repository.CandleRepository`. This
is purely an ingestion path: it resolves ``symbols.id`` via
:class:`~orvixa.db.repository.SymbolRepository` and batches
``CandleRepository.insert_batch`` the same way
:class:`orvixa.persistence.candles.CandleSink` does for the live feed -- no
analytics, signal, or schema changes.

Expected CSV columns (header row required)::

    ts, open, high, low, close, volume, quote_volume, trades, taker_buy_volume

``ts`` may be an ISO 8601 timestamp (e.g. ``2026-05-01T00:00:00+00:00``) or an
integer epoch in milliseconds. ``insert_batch`` upserts on
``(symbol_id, interval, ts)``, so re-running on the same file is idempotent.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..db.models import CandleRow
from ..db.repository import CandleRepository, SymbolRepository

DEFAULT_BATCH_SIZE = 1000


def _parse_ts(value: str) -> datetime:
    if value.lstrip("-").isdigit():
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    return datetime.fromisoformat(value)


def _row_to_candle_row(symbol_id: int, interval: str, row: dict[str, str]) -> CandleRow:
    return CandleRow(
        symbol_id=symbol_id,
        ts=_parse_ts(row["ts"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        quote_volume=float(row["quote_volume"]),
        trades=int(row["trades"]),
        taker_buy_volume=float(row["taker_buy_volume"]),
        interval=interval,
    )


async def load_candles_csv(
    pool: Any,
    base: str,
    csv_path: str | Path,
    interval: str = "1m",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Load a CSV of OHLCV candles for ``base`` into ``candles``; return rows written.

    ``base`` must already exist in ``symbols`` (the canonical display symbol,
    e.g. ``"BTC"``) -- raises :class:`ValueError` otherwise.
    """
    symbol_repo = SymbolRepository(pool)
    candle_repo = CandleRepository(pool)

    symbol_id = await symbol_repo.get_id(base)
    if symbol_id is None:
        raise ValueError(f"unknown symbol: {base!r} (not present in the symbols table)")

    written = 0
    batch: list[CandleRow] = []
    with open(csv_path, newline="") as f:  # noqa: ASYNC230 - local CLI batch load, not a server path
        reader = csv.DictReader(f)
        for row in reader:
            batch.append(_row_to_candle_row(symbol_id, interval, row))
            if len(batch) >= batch_size:
                written += await candle_repo.insert_batch(batch)
                batch = []
    if batch:
        written += await candle_repo.insert_batch(batch)
    return written
