"""M5 data foundation -- historical candle loaders for the ``candles`` table.

Currently one loader: :func:`load_candles_csv` (CSV/Parquet-style OHLCV file
-> ``candles``, via the existing :class:`~orvixa.db.repository.CandleRepository`).
"""

from __future__ import annotations

from .csv_loader import load_candles_csv

__all__ = ["load_candles_csv"]
