"""Initial M2 schema — symbols, candles/indicators hypertables, logs.

Implements the 8-table schema from the approved Phase 2 architecture
("05 Database schema"): the ``symbols`` registry, the ``candles`` and
``indicators`` TimescaleDB hypertables (with 7-day compression policies), and
the ``signals`` / ``market_events`` / ``market_memory`` / ``market_reports`` /
``telegram_alerts`` logs.

Revision ID: 0001
Revises:
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    # --- registry --------------------------------------------------------
    op.execute(
        """
        CREATE TABLE symbols (
            id serial PRIMARY KEY,
            symbol text NOT NULL UNIQUE,
            base text NOT NULL UNIQUE,
            quote text NOT NULL DEFAULT 'USDT',
            class text NOT NULL DEFAULT 'alt' CHECK (class IN ('core', 'alt', 'meme')),
            tier smallint NOT NULL DEFAULT 1,
            status text NOT NULL DEFAULT 'trading' CHECK (status IN ('trading', 'frozen')),
            tags text[] NOT NULL DEFAULT '{}',
            first_seen timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # --- candles hypertable -----------------------------------------------
    op.execute(
        """
        CREATE TABLE candles (
            symbol_id int NOT NULL REFERENCES symbols (id),
            ts timestamptz NOT NULL,
            interval text NOT NULL DEFAULT '1m',
            o numeric NOT NULL,
            h numeric NOT NULL,
            l numeric NOT NULL,
            c numeric NOT NULL,
            v numeric NOT NULL DEFAULT 0,
            quote_v numeric NOT NULL DEFAULT 0,
            trades int NOT NULL DEFAULT 0,
            taker_buy_v numeric NOT NULL DEFAULT 0,
            PRIMARY KEY (symbol_id, interval, ts)
        )
        """
    )
    op.execute("SELECT create_hypertable('candles', 'ts')")
    op.execute(
        """
        ALTER TABLE candles SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol_id, interval',
            timescaledb.compress_orderby = 'ts DESC'
        )
        """
    )
    op.execute("SELECT add_compression_policy('candles', INTERVAL '7 days')")

    # --- indicators hypertable ---------------------------------------------
    op.execute(
        """
        CREATE TABLE indicators (
            symbol_id int NOT NULL REFERENCES symbols (id),
            ts timestamptz NOT NULL,
            ema_fast numeric,
            ema_slow numeric,
            rsi numeric,
            atr numeric,
            vol_realized numeric,
            vol_rel numeric,
            trend_score numeric,
            PRIMARY KEY (symbol_id, ts)
        )
        """
    )
    op.execute("SELECT create_hypertable('indicators', 'ts')")
    op.execute(
        """
        ALTER TABLE indicators SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'symbol_id',
            timescaledb.compress_orderby = 'ts DESC'
        )
        """
    )
    op.execute("SELECT add_compression_policy('indicators', INTERVAL '7 days')")

    # --- signals -------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE signals (
            id bigserial PRIMARY KEY,
            symbol_id int NOT NULL REFERENCES symbols (id),
            ts timestamptz NOT NULL,
            type text NOT NULL CHECK (type IN ('buy', 'sell', 'highvol')),
            confidence smallint NOT NULL,
            components jsonb NOT NULL DEFAULT '{}',
            state_from text,
            state_to text
        )
        """
    )
    op.execute("CREATE INDEX signals_symbol_ts_idx ON signals (symbol_id, ts DESC)")

    # --- market_events ---------------------------------------------------------
    op.execute(
        """
        CREATE TABLE market_events (
            id bigserial PRIMARY KEY,
            symbol_id int NOT NULL REFERENCES symbols (id),
            ts timestamptz NOT NULL,
            type text NOT NULL CHECK (type IN ('pump', 'dump', 'breakout', 'breakdown', 'vol_spike')),
            magnitude numeric,
            severity smallint,
            price numeric,
            payload jsonb NOT NULL DEFAULT '{}'
        )
        """
    )
    op.execute("CREATE INDEX market_events_symbol_ts_idx ON market_events (symbol_id, ts DESC)")

    # --- market_memory -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE market_memory (
            id bigserial PRIMARY KEY,
            ts timestamptz NOT NULL,
            regime text,
            vol_regime text,
            breadth numeric,
            health_score smallint,
            snapshot jsonb NOT NULL DEFAULT '{}'
        )
        """
    )
    op.execute("CREATE INDEX market_memory_ts_idx ON market_memory (ts DESC)")

    # --- market_reports -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE market_reports (
            id bigserial PRIMARY KEY,
            ts timestamptz NOT NULL,
            regime text,
            scenarios jsonb NOT NULL DEFAULT '{}',
            headline text,
            body text,
            model text,
            tokens_used int,
            digest_hash text
        )
        """
    )
    op.execute("CREATE INDEX market_reports_ts_idx ON market_reports (ts DESC)")

    # --- telegram_alerts -----------------------------------------------------------
    op.execute(
        """
        CREATE TABLE telegram_alerts (
            id bigserial PRIMARY KEY,
            ref_type text NOT NULL CHECK (ref_type IN ('event', 'signal')),
            ref_id bigint NOT NULL,
            ts timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('sent', 'throttled', 'fail')),
            dedupe_key text,
            chat_id text,
            message text
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX telegram_alerts_dedupe_key_uniq "
        "ON telegram_alerts (dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    op.execute("CREATE INDEX telegram_alerts_ts_idx ON telegram_alerts (ts DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS telegram_alerts")
    op.execute("DROP TABLE IF EXISTS market_reports")
    op.execute("DROP TABLE IF EXISTS market_memory")
    op.execute("DROP TABLE IF EXISTS market_events")
    op.execute("DROP TABLE IF EXISTS signals")
    op.execute("DROP TABLE IF EXISTS indicators")
    op.execute("DROP TABLE IF EXISTS candles")
    op.execute("DROP TABLE IF EXISTS symbols")
