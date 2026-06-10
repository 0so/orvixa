"""Dynamic watchlist generation.

The ORVIXA watchlist is Tier 0 + Tier 1 — the symbols the feed actively
streams — sortable by volume, volatility, or daily change for the UI/API.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import RankedSymbol

_SORT_KEYS = {
    "volume": lambda r: r.quote_volume,
    "volatility": lambda r: abs(r.price_change_pct),
    "change": lambda r: r.price_change_pct,
}

WATCHLIST_TIERS = (0, 1)


def build_watchlist(symbols: Iterable[RankedSymbol]) -> list[RankedSymbol]:
    """Tier 0 + Tier 1 symbols — the dynamic ORVIXA watchlist."""
    return [s for s in symbols if s.tier in WATCHLIST_TIERS]


def sort_watchlist(
    symbols: Iterable[RankedSymbol], sort_by: str = "volume", *, descending: bool = True
) -> list[RankedSymbol]:
    """Sort watchlist symbols by ``"volume"``, ``"volatility"``, or ``"change"``."""
    key = _SORT_KEYS.get(sort_by)
    if key is None:
        raise ValueError(f"unknown sort_by {sort_by!r}; expected one of {sorted(_SORT_KEYS)}")
    return sorted(symbols, key=key, reverse=descending)
