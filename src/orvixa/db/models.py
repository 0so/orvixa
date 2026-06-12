"""Row models for the M2 schema (``alembic/versions/0001_initial_schema.py``).

Plain ``slots`` dataclasses — one per table that the platform writes to from
Python. They carry already-resolved foreign keys (``symbol_id``) and map 1:1
onto repository method parameters; no ORM.

Naming note: ``symbols.symbol`` stores the exchange pair (e.g. ``"BTCUSDT"``)
while ``symbols.base`` stores the canonical *display* symbol (e.g.
``"BTC"``) — the same value carried by ``Candle.symbol`` /
``TickerRow.symbol`` after :func:`orvixa.feeds.normalize.normalize_symbol`.
``base`` is the lookup key the rest of the platform uses; ``symbol`` is kept
for audit / future multi-exchange support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# --- registry ---------------------------------------------------------------

SYMBOL_CLASSES = ("core", "alt", "meme")
SYMBOL_STATUSES = ("trading", "frozen")


@dataclass(slots=True)
class SymbolRow:
    symbol: str  # exchange pair, e.g. "BTCUSDT"
    base: str  # display symbol, e.g. "BTC"
    quote: str = "USDT"
    klass: str = "alt"  # "core" | "alt" | "meme"
    tier: int = 1
    status: str = "trading"  # "trading" | "frozen"
    tags: list[str] = field(default_factory=list)


# --- time-series (hypertables) ----------------------------------------------


@dataclass(slots=True)
class CandleRow:
    symbol_id: int
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_volume: float
    interval: str = "1m"


@dataclass(slots=True)
class IndicatorRow:
    symbol_id: int
    ts: datetime
    ema_fast: float | None = None
    ema_slow: float | None = None
    rsi: float | None = None
    atr: float | None = None
    vol_realized: float | None = None
    vol_rel: float | None = None
    trend_score: float | None = None


# --- logs ---------------------------------------------------------------------

SIGNAL_TYPES = ("buy", "sell", "highvol")
EVENT_TYPES = ("pump", "dump", "breakout", "breakdown", "vol_spike")
ALERT_REF_TYPES = ("event", "signal")
ALERT_STATUSES = ("sent", "throttled", "fail")


@dataclass(slots=True)
class TierChangeRow:
    """One persisted tier/class transition from the Symbol Manager (M3).

    Mirrors :class:`orvixa.symbols.models.TierChange`, resolved to a
    ``symbols.id`` and timestamped at persistence time. This is the raw
    history the 30-day Market Intelligence evaluation's tiering component
    (the dominant signal in the decision matrix) is classified against.
    """

    symbol_id: int
    ts: datetime
    from_tier: int
    to_tier: int
    reason: str  # "ranking" | "spike" | "demote_spike" | "delisted" | "relisted"


@dataclass(slots=True)
class SignalRow:
    symbol_id: int
    ts: datetime
    type: str  # "buy" | "sell" | "highvol"
    confidence: int
    components: dict[str, Any] = field(default_factory=dict)
    state_from: str | None = None
    state_to: str | None = None


@dataclass(slots=True)
class MarketEventRow:
    symbol_id: int
    ts: datetime
    type: str
    magnitude: float | None = None
    severity: int | None = None
    price: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketMemoryRow:
    ts: datetime
    regime: str | None = None
    vol_regime: str | None = None
    breadth: float | None = None
    health_score: int | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MarketReportRow:
    ts: datetime
    regime: str | None = None
    scenarios: dict[str, Any] = field(default_factory=dict)
    headline: str | None = None
    body: str | None = None
    model: str | None = None
    tokens_used: int | None = None
    digest_hash: str | None = None


@dataclass(slots=True)
class TelegramAlertRow:
    ref_type: str  # "event" | "signal"
    ref_id: int
    ts: datetime
    status: str  # "sent" | "throttled" | "fail"
    dedupe_key: str | None = None
    chat_id: str | None = None
    message: str | None = None
