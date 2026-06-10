"""Add symbol ranking columns (Milestone 3)

Additive-only schema change for the Symbol Manager: adds ``rank`` (current
volume-rank position), ``metrics`` (latest 24h quote volume / change /
trade-count snapshot, jsonb), and ``last_synced`` (when the row was last
refreshed by the Symbol Manager) to ``symbols``. No existing column,
constraint, or table is touched — M1/M2 behavior is unchanged.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE symbols ADD COLUMN rank smallint")
    op.execute("ALTER TABLE symbols ADD COLUMN metrics jsonb NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE symbols ADD COLUMN last_synced timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE symbols DROP COLUMN last_synced")
    op.execute("ALTER TABLE symbols DROP COLUMN metrics")
    op.execute("ALTER TABLE symbols DROP COLUMN rank")
