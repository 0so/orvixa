"""Add price_change_dispersion and btc_dominance to breadth_snapshots

Additive-only schema change: extends the per-cycle breadth snapshot with two
cheap-to-compute market-regime proxies. ``price_change_dispersion`` is the
stddev of 24h price-change pct across the whole universe (a "everything
moves together" regime indicator). ``btc_dominance`` is BTC's share of total
24h quote volume across the universe.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE breadth_snapshots "
        "ADD COLUMN price_change_dispersion double precision NOT NULL DEFAULT 0, "
        "ADD COLUMN btc_dominance double precision NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE breadth_snapshots "
        "DROP COLUMN price_change_dispersion, "
        "DROP COLUMN btc_dominance"
    )
