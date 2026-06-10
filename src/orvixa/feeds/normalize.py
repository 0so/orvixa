"""Binance payload → internal model conversion.

The *only* module that knows Binance's wire format. Everything exchange-specific
(field names, the ``1000x`` meme multiplier, quote-asset suffixes) is isolated
here so the rest of the platform speaks pure ORVIXA models.
"""

from __future__ import annotations

from .base import Candle, TickerRow

# USDT first — it's the only quote ORVIXA tracks — then common alternates so a
# stray pair still normalizes sensibly.
_QUOTE_SUFFIXES = ("USDT", "FDUSD", "USDC", "BUSD", "TUSD")
_THOUSAND_PREFIX = "1000"


def normalize_symbol(pair: str) -> str:
    """``"BTCUSDT" -> "BTC"``, ``"1000PEPEUSDT" -> "PEPE"``.

    Strips the quote suffix and Binance's high-supply ``1000`` multiplier so the
    display layer shows the asset, never the exchange's pair name.
    """
    text = pair.upper()
    for suffix in _QUOTE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    if text.startswith(_THOUSAND_PREFIX) and len(text) > len(_THOUSAND_PREFIX):
        text = text[len(_THOUSAND_PREFIX):]
    return text


def to_stream_symbol(pair: str) -> str:
    """Binance combined-stream names are lower-case: ``"BTCUSDT" -> "btcusdt"``."""
    return pair.lower()


def kline_event_to_candle(data: dict) -> Candle:
    """Convert a ``@kline_1m`` WebSocket event payload to a :class:`Candle`."""
    k = data["k"]
    return Candle(
        symbol=normalize_symbol(data["s"]),
        ts=int(k["t"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        quote_volume=float(k["q"]),
        trades=int(k["n"]),
        closed=bool(k["x"]),
        taker_buy_volume=float(k.get("V", 0.0)),
    )


def rest_kline_to_candle(pair: str, row: list) -> Candle:
    """Convert one REST ``/api/v3/klines`` row to a finalized :class:`Candle`.

    Row layout: ``[openTime, open, high, low, close, volume, closeTime,
    quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]``.
    """
    return Candle(
        symbol=normalize_symbol(pair),
        ts=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
        quote_volume=float(row[7]),
        trades=int(row[8]),
        closed=True,
        taker_buy_volume=float(row[9]),
    )


def miniticker_array_to_rows(array: list[dict]) -> list[TickerRow]:
    """Convert a ``!miniTicker@arr`` payload to whole-market :class:`TickerRow` s.

    miniTicker carries 24h open (``o``) and last (``c``) but no percent field,
    so the 24h change is derived. Malformed entries are skipped, not fatal.
    """
    rows: list[TickerRow] = []
    for item in array:
        try:
            open_price = float(item["o"])
            last_price = float(item["c"])
            change = ((last_price - open_price) / open_price * 100.0) if open_price else 0.0
            rows.append(
                TickerRow(
                    symbol=normalize_symbol(item["s"]),
                    price=last_price,
                    change_pct=change,
                    quote_volume=float(item["q"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return rows
