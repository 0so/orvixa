"""Add breadth_snapshots log (raw per-cycle market breadth)

Additive-only schema change: persists one row per Symbol Manager refresh
cycle (~5 min) with the whole-market breadth metrics already computed by
BreadthEngine (advancers, decliners, ad_ratio, pct_above_trend, new highs/
lows). Aggregate counterpart to ``symbol_metrics_snapshots`` (0004) — pure
data collection for testing market-regime / mean-reversion hypotheses on
breadth itself.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE breadth_snapshots (
            id bigserial PRIMARY KEY,
            ts timestamptz NOT NULL,
            total int NOT NULL,
            advancers int NOT NULL,
            decliners int NOT NULL,
            unchanged int NOT NULL,
            ad_ratio double precision NOT NULL,
            pct_above_trend double precision NOT NULL,
            new_highs int NOT NULL,
            new_lows int NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX breadth_snapshots_ts_idx ON breadth_snapshots (ts DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE breadth_snapshots")
