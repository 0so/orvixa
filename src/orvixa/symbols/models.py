"""Data models for the Symbol Manager (Milestone 3).

Mirrors the M1 pattern (`feeds/base.py`): plain ``slots`` dataclasses that the
rest of the platform speaks, isolating Binance's `/exchangeInfo` and
`/ticker/24hr` payload shapes to :mod:`orvixa.symbols.client`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..feeds.normalize import normalize_symbol


@dataclass(slots=True)
class ExchangeSymbol:
    """One tradable pair from ``/api/v3/exchangeInfo``."""

    pair: str  # exchange pair, e.g. "BTCUSDT"
    base_asset: str  # raw base asset, e.g. "BTC" (or "1000PEPE")
    quote_asset: str  # e.g. "USDT"
    status: str  # "TRADING", "BREAK", "HALT", ...

    @property
    def base(self) -> str:
        """Canonical display symbol (e.g. ``"1000PEPEUSDT"`` -> ``"PEPE"``)."""
        return normalize_symbol(self.pair)


@dataclass(slots=True)
class TickerStats:
    """24h rolling stats for one pair from ``/api/v3/ticker/24hr``."""

    pair: str
    last_price: float
    quote_volume: float
    price_change_pct: float
    count: int  # number of trades in the 24h window

    @property
    def base(self) -> str:
        return normalize_symbol(self.pair)


@dataclass(slots=True)
class RankedSymbol:
    """A symbol with its current tier/rank/metrics — the watchlist row shape."""

    base: str
    pair: str
    tier: int
    klass: str
    status: str
    rank: int | None
    quote_volume: float
    price_change_pct: float
    last_price: float
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BreadthSnapshot:
    """Market-wide breadth metrics derived from a whole-market ticker snapshot."""

    total: int
    advancers: int
    decliners: int
    unchanged: int
    ad_ratio: float
    pct_above_trend: float
    new_highs: int
    new_lows: int


@dataclass(slots=True)
class TierChange:
    """A tier/class transition produced by one refresh cycle."""

    base: str
    pair: str
    from_tier: int
    to_tier: int
    reason: str  # "ranking" | "spike" | "demote_spike" | "delisted" | "relisted"
