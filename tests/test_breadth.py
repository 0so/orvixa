"""Tests for :mod:`orvixa.symbols.breadth`."""

from __future__ import annotations

from orvixa.feeds.base import TickerRow
from orvixa.symbols.breadth import BreadthEngine


def _row(symbol: str, price: float, change_pct: float) -> TickerRow:
    return TickerRow(symbol=symbol, price=price, change_pct=change_pct, quote_volume=1000.0)


def test_advancers_decliners_unchanged_and_ratio() -> None:
    engine = BreadthEngine(trend_window=5)
    rows = [
        _row("BTC", 100.0, 1.0),
        _row("ETH", 50.0, -2.0),
        _row("SOL", 10.0, 0.0),
        _row("BNB", 20.0, 3.0),
    ]

    snap = engine.update(rows)

    assert snap.total == 4
    assert snap.advancers == 2
    assert snap.decliners == 1
    assert snap.unchanged == 1
    assert snap.ad_ratio == 2.0  # 2 advancers / 1 decliner


def test_ad_ratio_with_no_decliners_returns_advancer_count() -> None:
    engine = BreadthEngine(trend_window=5)
    rows = [_row("BTC", 100.0, 1.0), _row("ETH", 50.0, 2.0)]

    snap = engine.update(rows)

    assert snap.decliners == 0
    assert snap.ad_ratio == 2.0


def test_first_snapshot_has_no_trend_or_highs_lows() -> None:
    engine = BreadthEngine(trend_window=5)
    rows = [_row("BTC", 100.0, 1.0), _row("ETH", 50.0, -1.0)]

    snap = engine.update(rows)

    # No history yet, so nothing can be "above trend" or a new high/low.
    assert snap.pct_above_trend == 0.0
    assert snap.new_highs == 0
    assert snap.new_lows == 0


def test_pct_above_trend_and_new_highs_lows_after_history_builds() -> None:
    engine = BreadthEngine(trend_window=5)

    # Seed history: BTC oscillates around 100, ETH trends down from 50.
    engine.update([_row("BTC", 100.0, 0.0), _row("ETH", 50.0, 0.0)])
    engine.update([_row("BTC", 90.0, 0.0), _row("ETH", 48.0, 0.0)])
    engine.update([_row("BTC", 110.0, 0.0), _row("ETH", 46.0, 0.0)])

    # BTC jumps to a new high above its rolling average and prior max.
    # ETH drops to a new low below its rolling average and prior min.
    snap = engine.update([_row("BTC", 200.0, 5.0), _row("ETH", 10.0, -5.0)])

    assert snap.new_highs == 1  # BTC
    assert snap.new_lows == 1  # ETH
    assert snap.pct_above_trend == 50.0  # only BTC is above its trailing average
