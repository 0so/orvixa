"""``backfill`` — load historical CSV candles into the ``candles`` table.

Usage::

    orvixa-backfill <dir> [--interval 1m]

``<dir>`` contains one CSV per symbol, named ``<BASE>.csv`` (e.g.
``BTC.csv``, ``ETH.csv``) -- ``<BASE>`` must already exist in ``symbols``.
Each file is loaded via :func:`orvixa.backfill.load_candles_csv`. No feed,
analytics, or signal-validation code is touched.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ..backfill import load_candles_csv
from ..config import get_settings
from ..db import create_pool
from ..logging import get_logger, setup_logging


async def run(directory: Path, interval: str) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    log = get_logger("orvixa.backfill")

    pool = await create_pool(settings)
    try:
        for csv_path in sorted(directory.glob("*.csv")):  # noqa: ASYNC240 - local CLI, not a server path
            base = csv_path.stem
            written = await load_candles_csv(pool, base, csv_path, interval=interval)
            log.info("backfilled candles", extra={"symbol": base, "rows": written})
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load historical OHLCV CSVs into candles")
    parser.add_argument("directory", type=Path, help="Directory of <BASE>.csv files")
    parser.add_argument("--interval", default="1m", help="candles.interval value (default: 1m)")
    args = parser.parse_args()
    asyncio.run(run(args.directory, args.interval))


if __name__ == "__main__":
    main()
