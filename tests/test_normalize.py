"""Normalization is the only Binance-aware code — lock it hard."""

from __future__ import annotations

import pytest

from orvixa.feeds.normalize import (
    kline_event_to_candle,
    miniticker_array_to_rows,
    normalize_symbol,
    rest_kline_to_candle,
    to_stream_symbol,
)


@pytest.mark.parametrize(
    "pair,expected",
    [
        ("BTCUSDT", "BTC"),
        ("ETHUSDT", "ETH"),
        ("1000PEPEUSDT", "PEPE"),
        ("1000SHIBUSDT", "SHIB"),
        ("WIFUSDT", "WIF"),
        ("btcusdt", "BTC"),       # case-insensitive
        ("ETHFDUSD", "ETH"),      # alternate quote suffix
        ("SOLUSDC", "SOL"),
    ],
)
def test_normalize_symbol(pair: str, expected: str) -> None:
    assert normalize_symbol(pair) == expected


def test_to_stream_symbol() -> None:
    assert to_stream_symbol("BTCUSDT") == "btcusdt"


def test_kline_event_to_candle(kline_event: dict) -> None:
    candle = kline_event_to_candle(kline_event)
    assert candle.symbol == "PEPE"
    assert candle.ts == 1718030400000
    assert candle.open == pytest.approx(0.01284)
    assert candle.high == pytest.approx(0.01296)
    assert candle.low == pytest.approx(0.01282)
    assert candle.close == pytest.approx(0.01291)
    assert candle.volume == pytest.approx(184320000.0)
    assert candle.quote_volume == pytest.approx(2378940.55)
    assert candle.trades == 121
    assert candle.closed is True
    assert candle.is_bullish is True
    assert candle.taker_buy_volume == pytest.approx(92160000.0)


def test_kline_open_flag_when_not_closed(kline_event: dict) -> None:
    kline_event["k"]["x"] = False
    candle = kline_event_to_candle(kline_event)
    assert candle.closed is False


def test_rest_kline_to_candle() -> None:
    row = [
        1718030400000, "0.01284", "0.01296", "0.01282", "0.01291",
        "184320000", 1718030459999, "2378940.55", 121, "92160000", "1189470.27", "0",
    ]
    candle = rest_kline_to_candle("1000PEPEUSDT", row)
    assert candle.symbol == "PEPE"
    assert candle.ts == 1718030400000
    assert candle.close == pytest.approx(0.01291)
    assert candle.quote_volume == pytest.approx(2378940.55)
    assert candle.trades == 121
    assert candle.closed is True
    assert candle.taker_buy_volume == pytest.approx(92160000.0)


def test_miniticker_array_to_rows(miniticker_array: list[dict]) -> None:
    rows = miniticker_array_to_rows(miniticker_array)
    # the malformed row is dropped
    assert len(rows) == 3
    by_symbol = {r.symbol: r for r in rows}
    assert set(by_symbol) == {"BTC", "ETH", "PEPE"}
    # change% is derived from open/last
    assert by_symbol["BTC"].change_pct == pytest.approx((69000 - 68000) / 68000 * 100)
    assert by_symbol["ETH"].change_pct < 0  # 3500 < 3550
    assert by_symbol["BTC"].quote_volume == pytest.approx(1.2e9)
