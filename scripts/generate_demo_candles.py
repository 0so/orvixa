"""Generate synthetic 1m OHLCV CSVs for the M5 data-foundation backfill.

This environment has no outbound access to a real exchange API, so there is
no real historical OHLCV data available to load. This script produces
deterministic (seeded), synthetic 1m candles for a fixed list of symbols
covering a configurable number of days, in the CSV format expected by
:func:`orvixa.backfill.load_candles_csv`
(``ts, open, high, low, close, volume, quote_volume, trades, taker_buy_volume``).

Output is for populating the data-foundation layer (and exercising the
loader/replay pipeline end to end) -- it is NOT real market data and must not
be used to draw conclusions about signal edge.

Usage::

    python scripts/generate_demo_candles.py <output_dir> [--days 30]
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

# base, starting price, per-candle volatility (std dev of log-return)
SYMBOLS: list[tuple[str, float, float]] = [
    ("BTC", 60000.0, 0.0006),
    ("ETH", 3000.0, 0.0008),
    ("SOL", 140.0, 0.0012),
    ("BNB", 550.0, 0.0007),
    ("XRP", 0.55, 0.0010),
    ("AVAX", 35.0, 0.0011),
    ("LINK", 15.0, 0.0010),
    ("DOGE", 0.15, 0.0015),
    ("PEPE", 0.000012, 0.0020),
    ("SHIB", 0.000022, 0.0018),
    ("WIF", 2.5, 0.0020),
    ("TRX", 0.12, 0.0007),
]

MINUTES_PER_DAY = 24 * 60


def _generate_symbol(base: str, start_price: float, sigma: float, minutes: int, seed: int):
    rng = random.Random(seed)
    start = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(minutes=minutes)

    price = start_price
    rows = []
    for i in range(minutes):
        ts = start + timedelta(minutes=i)
        open_ = price
        # Random-walk close via a small log-return; mild mean reversion to
        # keep prices in a sane band over 30 days.
        drift = -0.00002 * ((price / start_price) - 1.0)
        log_return = rng.gauss(drift, sigma)
        close = open_ * (1.0 + log_return)

        spread = abs(rng.gauss(0, sigma)) * open_
        high = max(open_, close) + spread
        low = min(open_, close) - spread
        low = max(low, 0.0)

        volume = abs(rng.gauss(100.0, 20.0))
        quote_volume = volume * (open_ + close) / 2.0
        trades = max(1, int(abs(rng.gauss(50, 15))))
        taker_buy_volume = volume * rng.uniform(0.3, 0.7)

        rows.append(
            [
                ts.isoformat(),
                f"{open_:.10g}",
                f"{high:.10g}",
                f"{low:.10g}",
                f"{close:.10g}",
                f"{volume:.6f}",
                f"{quote_volume:.6f}",
                trades,
                f"{taker_buy_volume:.6f}",
            ]
        )
        price = close

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic demo OHLCV CSVs")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    minutes = args.days * MINUTES_PER_DAY

    header = [
        "ts",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_volume",
    ]

    for seed, (base, start_price, sigma) in enumerate(SYMBOLS):
        rows = _generate_symbol(base, start_price, sigma, minutes, seed=seed)
        out_path = args.output_dir / f"{base}.csv"
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        print(f"{base}: {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
