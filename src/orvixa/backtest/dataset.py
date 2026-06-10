"""Dataset provenance classification -- REAL vs SYNTHETIC.

Signal validation (and any future backtesting) must know whether the candles
it is replaying are real exchange data or the synthetic seeded-random-walk
data produced by ``scripts/generate_demo_candles.py`` (see ``DATASET.md``).
Provenance is read from ``symbols.tags``: any symbol tagged
``"synthetic_data"`` marks the dataset as synthetic for that run.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..db.repository import SymbolRepository

SYNTHETIC_TAG = "synthetic_data"

REAL = "REAL"
SYNTHETIC = "SYNTHETIC"


async def classify_dataset(symbol_repo: SymbolRepository, symbols: Sequence[str]) -> str:
    """Return :data:`SYNTHETIC` if any of ``symbols`` is tagged ``synthetic_data``.

    Otherwise return :data:`REAL`. ``symbols`` are canonical display symbols
    (``symbols.base``, e.g. ``"BTC"``).
    """
    rows = await symbol_repo.list_all()
    tags_by_base = {row["base"]: list(row["tags"] or []) for row in rows}
    for base in symbols:
        if SYNTHETIC_TAG in tags_by_base.get(base, []):
            return SYNTHETIC
    return REAL
