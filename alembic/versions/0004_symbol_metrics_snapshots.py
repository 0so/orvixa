"""Add symbol_metrics_snapshots log (raw per-cycle 24h metrics)

Additive-only schema change: persists a raw snapshot of each symbol's 24h
quote volume, price change %, trade count, last price, and current tier on
every Symbol Manager refresh cycle (~5 min). Pure data collection — no
thresholds applied — building the dataset needed to design an adaptive
(EWMA z-score) anomaly-vs-noise signal to replace the fixed-threshold
spike promotion/demotion logic.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE symbol_metrics_snapshots (
            id bigserial PRIMARY KEY,
            symbol_id int NOT NULL REFERENCES symbols (id),
            ts timestamptz NOT NULL,
            tier smallint NOT NULL,
            quote_volume_24h double precision NOT NULL,
            price_change_pct_24h double precision NOT NULL,
            trade_count_24h int NOT NULL,
            last_price double precision NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX symbol_metrics_snapshots_symbol_ts_idx "
        "ON symbol_metrics_snapshots (symbol_id, ts DESC)"
    )
    op.execute("CREATE INDEX symbol_metrics_snapshots_ts_idx ON symbol_metrics_snapshots (ts DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE symbol_metrics_snapshots")
