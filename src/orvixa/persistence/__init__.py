"""Async persistence helpers (Milestone 2).

``batch_writer.py`` provides the generic, size/time-triggered
:class:`~orvixa.persistence.batch_writer.BatchWriter`; ``candles.py`` wires
the M1 :class:`~orvixa.feeds.base.MarketFeed` candle-close callback into the
``candles`` hypertable via the repository layer in :mod:`orvixa.db`.
"""

from __future__ import annotations

from .batch_writer import BatchWriter
from .candles import CandleSink, candle_repository_sink
from .registry import build_symbol_rows, seed_symbols

__all__ = [
    "BatchWriter",
    "CandleSink",
    "candle_repository_sink",
    "build_symbol_rows",
    "seed_symbols",
]
