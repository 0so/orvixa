"""Tests for :mod:`orvixa.symbols.watchlist`."""

from __future__ import annotations

import pytest

from orvixa.symbols.models import RankedSymbol
from orvixa.symbols.watchlist import build_watchlist, sort_watchlist


def _ranked(base: str, tier: int, quote_volume: float, change_pct: float) -> RankedSymbol:
    return RankedSymbol(
        base=base,
        pair=f"{base}USDT",
        tier=tier,
        klass="alt",
        status="trading",
        rank=None,
        quote_volume=quote_volume,
        price_change_pct=change_pct,
        last_price=1.0,
    )


def test_build_watchlist_keeps_only_tier0_and_tier1() -> None:
    symbols = [
        _ranked("BTC", 0, 1_000_000, 1.0),
        _ranked("LINK", 1, 500_000, -2.0),
        _ranked("RANDOM", 2, 10, 0.0),
    ]

    watchlist = build_watchlist(symbols)

    assert {s.base for s in watchlist} == {"BTC", "LINK"}


def test_sort_watchlist_by_volume() -> None:
    symbols = [
        _ranked("BTC", 0, 1_000_000, 1.0),
        _ranked("ETH", 0, 2_000_000, -1.0),
        _ranked("SOL", 1, 500_000, 5.0),
    ]

    sorted_syms = sort_watchlist(symbols, sort_by="volume")

    assert [s.base for s in sorted_syms] == ["ETH", "BTC", "SOL"]


def test_sort_watchlist_by_volatility_uses_absolute_change() -> None:
    symbols = [
        _ranked("BTC", 0, 1_000_000, 1.0),
        _ranked("ETH", 0, 1_000_000, -8.0),
        _ranked("SOL", 1, 1_000_000, 5.0),
    ]

    sorted_syms = sort_watchlist(symbols, sort_by="volatility")

    assert [s.base for s in sorted_syms] == ["ETH", "SOL", "BTC"]


def test_sort_watchlist_by_change_signed() -> None:
    symbols = [
        _ranked("BTC", 0, 1_000_000, 1.0),
        _ranked("ETH", 0, 1_000_000, -8.0),
        _ranked("SOL", 1, 1_000_000, 5.0),
    ]

    sorted_syms = sort_watchlist(symbols, sort_by="change")

    assert [s.base for s in sorted_syms] == ["SOL", "BTC", "ETH"]


def test_sort_watchlist_ascending() -> None:
    symbols = [
        _ranked("BTC", 0, 1_000_000, 1.0),
        _ranked("ETH", 0, 2_000_000, -1.0),
    ]

    sorted_syms = sort_watchlist(symbols, sort_by="volume", descending=False)

    assert [s.base for s in sorted_syms] == ["BTC", "ETH"]


def test_sort_watchlist_unknown_key_raises() -> None:
    with pytest.raises(ValueError):
        sort_watchlist([], sort_by="bogus")
