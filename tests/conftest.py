"""Shared pytest fixtures and helpers for the M1 feed tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def kline_event() -> dict:
    """The recorded Binance ``@kline_1m`` event payload (``data`` object)."""
    raw = json.loads((FIXTURES / "kline_1m.json").read_text())
    return raw["data"]


@pytest.fixture
def miniticker_array() -> list[dict]:
    """A small synthetic ``!miniTicker@arr`` payload."""
    return [
        {"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "69000.0", "o": "68000.0", "q": "1200000000"},
        {"e": "24hrMiniTicker", "s": "ETHUSDT", "c": "3500.0", "o": "3550.0", "q": "600000000"},
        {"e": "24hrMiniTicker", "s": "1000PEPEUSDT", "c": "0.0130", "o": "0.0125", "q": "90000000"},
        {"s": "BROKEN"},  # malformed → must be skipped, not fatal
    ]
