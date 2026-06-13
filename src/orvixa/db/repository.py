"""Repository layer — one class per table in the M2 schema.

Every repository takes a :class:`~orvixa.db.pool.DBPool` (asyncpg's ``Pool``,
or a fake exposing the same async methods) and speaks only in the row models
from :mod:`orvixa.db.models`. SQL lives here and nowhere else.

``candles`` and the symbol registry are the two pieces M2 actively writes to
(see :mod:`orvixa.persistence.candles`); the remaining repositories provide
the schema + CRUD surface that M4 (indicators/signals/events), M6
(market_memory/market_reports) and M7 (telegram_alerts) build on.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from .models import (
    BreadthSnapshotRow,
    CandleRow,
    IndicatorRow,
    MarketEventRow,
    MarketMemoryRow,
    MarketReportRow,
    SignalRow,
    SymbolMetricsSnapshotRow,
    SymbolRow,
    TelegramAlertRow,
    TierChangeRow,
)
from .pool import DBPool


class SymbolRepository:
    """The ``symbols`` registry — the FK target for every other table."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def upsert(self, row: SymbolRow) -> int:
        """Insert or update a symbol, keyed on the canonical ``base`` symbol.

        Returns the ``symbols.id``. Existing ``class``/``tier``/``status``/
        ``tags`` are refreshed; ``first_seen`` is left untouched.
        """
        result = await self._pool.fetchrow(
            """
            INSERT INTO symbols (symbol, base, quote, class, tier, status, tags)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (base) DO UPDATE SET
                symbol = EXCLUDED.symbol,
                quote = EXCLUDED.quote,
                class = EXCLUDED.class,
                tier = EXCLUDED.tier,
                status = EXCLUDED.status,
                tags = EXCLUDED.tags
            RETURNING id
            """,
            row.symbol,
            row.base,
            row.quote,
            row.klass,
            row.tier,
            row.status,
            row.tags,
        )
        assert result is not None
        return int(result["id"])

    async def ensure_seeded(self, rows: Sequence[SymbolRow]) -> dict[str, int]:
        """Upsert every row, returning a ``base -> symbols.id`` map."""
        out: dict[str, int] = {}
        for row in rows:
            out[row.base] = await self.upsert(row)
        return out

    async def get_id(self, base: str) -> int | None:
        value = await self._pool.fetchval("SELECT id FROM symbols WHERE base = $1", base)
        return int(value) if value is not None else None

    async def list_all(self):
        return await self._pool.fetch("SELECT * FROM symbols ORDER BY id")

    async def update_ranking(self, base: str, rank: int | None, metrics: dict[str, Any]) -> None:
        """Refresh the Symbol Manager's volume rank + latest 24h metrics snapshot."""
        await self._pool.execute(
            "UPDATE symbols SET rank = $1, metrics = $2::jsonb, last_synced = now() WHERE base = $3",
            rank,
            json.dumps(metrics),
            base,
        )


class TierChangeRepository:
    """The ``tier_changes`` log — every tier/class transition from M3."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert(self, row: TierChangeRow) -> int:
        result = await self._pool.fetchrow(
            """
            INSERT INTO tier_changes (symbol_id, ts, from_tier, to_tier, reason)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            row.symbol_id,
            row.ts,
            row.from_tier,
            row.to_tier,
            row.reason,
        )
        assert result is not None
        return int(result["id"])

    async def get_recent(self, symbol_id: int | None = None, limit: int = 50):
        if symbol_id is None:
            return await self._pool.fetch(
                "SELECT * FROM tier_changes ORDER BY ts DESC LIMIT $1", limit
            )
        return await self._pool.fetch(
            "SELECT * FROM tier_changes WHERE symbol_id = $1 ORDER BY ts DESC LIMIT $2",
            symbol_id,
            limit,
        )


class SymbolMetricsSnapshotRepository:
    """The ``symbol_metrics_snapshots`` log — raw 24h metrics per refresh cycle.

    Append-only dataset for studying anomaly-vs-noise patterns ahead of an
    adaptive promotion/demotion signal.
    """

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert_batch(self, rows: Sequence[SymbolMetricsSnapshotRow]) -> int:
        if not rows:
            return 0
        await self._pool.executemany(
            """
            INSERT INTO symbol_metrics_snapshots
                (symbol_id, ts, tier, quote_volume_24h, price_change_pct_24h, trade_count_24h, last_price)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            [
                (
                    r.symbol_id,
                    r.ts,
                    r.tier,
                    r.quote_volume_24h,
                    r.price_change_pct_24h,
                    r.trade_count_24h,
                    r.last_price,
                )
                for r in rows
            ],
        )
        return len(rows)

    async def get_recent(self, symbol_id: int, limit: int = 200):
        return await self._pool.fetch(
            """
            SELECT * FROM symbol_metrics_snapshots
            WHERE symbol_id = $1
            ORDER BY ts DESC
            LIMIT $2
            """,
            symbol_id,
            limit,
        )


class BreadthSnapshotRepository:
    """The ``breadth_snapshots`` log — one row per refresh cycle, whole-market.

    Aggregate counterpart to :class:`SymbolMetricsSnapshotRepository`. Append-
    only dataset for market-regime / mean-reversion research on breadth.
    """

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert(self, row: BreadthSnapshotRow) -> None:
        await self._pool.execute(
            """
            INSERT INTO breadth_snapshots
                (ts, total, advancers, decliners, unchanged, ad_ratio, pct_above_trend, new_highs, new_lows)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            """,
            row.ts,
            row.total,
            row.advancers,
            row.decliners,
            row.unchanged,
            row.ad_ratio,
            row.pct_above_trend,
            row.new_highs,
            row.new_lows,
        )

    async def get_recent(self, limit: int = 200):
        return await self._pool.fetch(
            "SELECT * FROM breadth_snapshots ORDER BY ts DESC LIMIT $1", limit
        )


class CandleRepository:
    """The ``candles`` hypertable."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert_batch(self, rows: Sequence[CandleRow]) -> int:
        """Upsert a batch of candles (idempotent on ``(symbol_id, interval, ts)``).

        A re-delivered or re-backfilled candle for the same minute overwrites
        the previous row rather than erroring or duplicating — important
        because gap-fill and live updates can both touch the same bar.
        """
        if not rows:
            return 0
        await self._pool.executemany(
            """
            INSERT INTO candles
                (symbol_id, ts, interval, o, h, l, c, v, quote_v, trades, taker_buy_v)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (symbol_id, interval, ts) DO UPDATE SET
                o = EXCLUDED.o,
                h = EXCLUDED.h,
                l = EXCLUDED.l,
                c = EXCLUDED.c,
                v = EXCLUDED.v,
                quote_v = EXCLUDED.quote_v,
                trades = EXCLUDED.trades,
                taker_buy_v = EXCLUDED.taker_buy_v
            """,
            [
                (
                    r.symbol_id,
                    r.ts,
                    r.interval,
                    r.open,
                    r.high,
                    r.low,
                    r.close,
                    r.volume,
                    r.quote_volume,
                    r.trades,
                    r.taker_buy_volume,
                )
                for r in rows
            ],
        )
        return len(rows)

    async def select_range(
        self,
        symbol_id: int,
        interval: str = "1m",
        start: datetime | None = None,
        end: datetime | None = None,
    ):
        """Candles for one symbol, ascending by ``ts`` (optionally bounded).

        Used by the M5 signal-validation harness to load a deterministic,
        ordered candle history for replay. ``start``/``end`` are inclusive;
        either may be ``None`` for an open-ended bound.
        """
        return await self._pool.fetch(
            """
            SELECT symbol_id, ts, interval, o, h, l, c, v, quote_v, trades, taker_buy_v
            FROM candles
            WHERE symbol_id = $1 AND interval = $2
              AND ($3::timestamptz IS NULL OR ts >= $3)
              AND ($4::timestamptz IS NULL OR ts <= $4)
            ORDER BY ts ASC
            """,
            symbol_id,
            interval,
            start,
            end,
        )

    async def get_recent(self, symbol_id: int, interval: str = "1m", limit: int = 100):
        """Most recent ``limit`` candles for a symbol, newest first."""
        return await self._pool.fetch(
            """
            SELECT symbol_id, ts, interval, o, h, l, c, v, quote_v, trades, taker_buy_v
            FROM candles
            WHERE symbol_id = $1 AND interval = $2
            ORDER BY ts DESC
            LIMIT $3
            """,
            symbol_id,
            interval,
            limit,
        )


class IndicatorRepository:
    """The ``indicators`` hypertable (populated from M4)."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def upsert(self, row: IndicatorRow) -> None:
        await self._pool.execute(
            """
            INSERT INTO indicators
                (symbol_id, ts, ema_fast, ema_slow, rsi, atr, vol_realized, vol_rel, trend_score)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (symbol_id, ts) DO UPDATE SET
                ema_fast = EXCLUDED.ema_fast,
                ema_slow = EXCLUDED.ema_slow,
                rsi = EXCLUDED.rsi,
                atr = EXCLUDED.atr,
                vol_realized = EXCLUDED.vol_realized,
                vol_rel = EXCLUDED.vol_rel,
                trend_score = EXCLUDED.trend_score
            """,
            row.symbol_id,
            row.ts,
            row.ema_fast,
            row.ema_slow,
            row.rsi,
            row.atr,
            row.vol_realized,
            row.vol_rel,
            row.trend_score,
        )

    async def upsert_batch(self, rows: Sequence[IndicatorRow]) -> int:
        """Batched upsert (one round trip for many symbols' latest indicators).

        Same idempotent ``(symbol_id, ts)`` upsert as :meth:`upsert`, via
        ``executemany`` — the M4 analytics engine computes one row per
        tracked symbol per candle close and flushes them together so a
        universe of hundreds of symbols doesn't cost hundreds of round trips.
        """
        if not rows:
            return 0
        await self._pool.executemany(
            """
            INSERT INTO indicators
                (symbol_id, ts, ema_fast, ema_slow, rsi, atr, vol_realized, vol_rel, trend_score)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (symbol_id, ts) DO UPDATE SET
                ema_fast = EXCLUDED.ema_fast,
                ema_slow = EXCLUDED.ema_slow,
                rsi = EXCLUDED.rsi,
                atr = EXCLUDED.atr,
                vol_realized = EXCLUDED.vol_realized,
                vol_rel = EXCLUDED.vol_rel,
                trend_score = EXCLUDED.trend_score
            """,
            [
                (
                    r.symbol_id,
                    r.ts,
                    r.ema_fast,
                    r.ema_slow,
                    r.rsi,
                    r.atr,
                    r.vol_realized,
                    r.vol_rel,
                    r.trend_score,
                )
                for r in rows
            ],
        )
        return len(rows)

    async def get_latest(self, symbol_id: int):
        return await self._pool.fetchrow(
            """
            SELECT symbol_id, ts, ema_fast, ema_slow, rsi, atr, vol_realized, vol_rel, trend_score
            FROM indicators
            WHERE symbol_id = $1
            ORDER BY ts DESC
            LIMIT 1
            """,
            symbol_id,
        )


class SignalRepository:
    """The ``signals`` log (populated from M4)."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert(self, row: SignalRow) -> int:
        result = await self._pool.fetchrow(
            """
            INSERT INTO signals (symbol_id, ts, type, confidence, components, state_from, state_to)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            RETURNING id
            """,
            row.symbol_id,
            row.ts,
            row.type,
            row.confidence,
            json.dumps(row.components),
            row.state_from,
            row.state_to,
        )
        assert result is not None
        return int(result["id"])

    async def get_recent(self, symbol_id: int | None = None, limit: int = 50):
        if symbol_id is None:
            return await self._pool.fetch(
                "SELECT * FROM signals ORDER BY ts DESC LIMIT $1", limit
            )
        return await self._pool.fetch(
            "SELECT * FROM signals WHERE symbol_id = $1 ORDER BY ts DESC LIMIT $2",
            symbol_id,
            limit,
        )


class MarketEventRepository:
    """The ``market_events`` log (populated from M4)."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert(self, row: MarketEventRow) -> int:
        result = await self._pool.fetchrow(
            """
            INSERT INTO market_events (symbol_id, ts, type, magnitude, severity, price, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            row.symbol_id,
            row.ts,
            row.type,
            row.magnitude,
            row.severity,
            row.price,
            json.dumps(row.payload),
        )
        assert result is not None
        return int(result["id"])

    async def get_recent(self, symbol_id: int | None = None, limit: int = 50):
        if symbol_id is None:
            return await self._pool.fetch(
                "SELECT * FROM market_events ORDER BY ts DESC LIMIT $1", limit
            )
        return await self._pool.fetch(
            "SELECT * FROM market_events WHERE symbol_id = $1 ORDER BY ts DESC LIMIT $2",
            symbol_id,
            limit,
        )


class MarketMemoryRepository:
    """The ``market_memory`` snapshot log (populated from M6)."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert_snapshot(self, row: MarketMemoryRow) -> int:
        result = await self._pool.fetchrow(
            """
            INSERT INTO market_memory (ts, regime, vol_regime, breadth, health_score, snapshot)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            RETURNING id
            """,
            row.ts,
            row.regime,
            row.vol_regime,
            row.breadth,
            row.health_score,
            json.dumps(row.snapshot),
        )
        assert result is not None
        return int(result["id"])

    async def get_recent(self, limit: int = 50):
        return await self._pool.fetch(
            "SELECT * FROM market_memory ORDER BY ts DESC LIMIT $1", limit
        )


class MarketReportRepository:
    """The ``market_reports`` log (populated from M6)."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert(self, row: MarketReportRow) -> int:
        result = await self._pool.fetchrow(
            """
            INSERT INTO market_reports
                (ts, regime, scenarios, headline, body, model, tokens_used, digest_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
            """,
            row.ts,
            row.regime,
            row.scenarios,
            row.headline,
            row.body,
            row.model,
            row.tokens_used,
            row.digest_hash,
        )
        assert result is not None
        return int(result["id"])

    async def get_latest(self):
        return await self._pool.fetchrow("SELECT * FROM market_reports ORDER BY ts DESC LIMIT 1")


class TelegramAlertRepository:
    """The ``telegram_alerts`` outbox (populated from M7)."""

    def __init__(self, pool: DBPool) -> None:
        self._pool = pool

    async def insert(self, row: TelegramAlertRow) -> int:
        result = await self._pool.fetchrow(
            """
            INSERT INTO telegram_alerts (ref_type, ref_id, ts, status, dedupe_key, chat_id, message)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
            RETURNING id
            """,
            row.ref_type,
            row.ref_id,
            row.ts,
            row.status,
            row.dedupe_key,
            row.chat_id,
            row.message,
        )
        return int(result["id"]) if result is not None else -1

    async def get_recent(self, limit: int = 50):
        return await self._pool.fetch(
            "SELECT * FROM telegram_alerts ORDER BY ts DESC LIMIT $1", limit
        )
