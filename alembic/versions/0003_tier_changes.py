"""Add tier_changes log (30-day Market Intelligence evaluation)

Additive-only schema change: persists every tier/class transition produced
by :class:`~orvixa.symbols.manager.SymbolManager.refresh_universe` (M3) as a
:class:`~orvixa.db.models.TierChangeRow`. Previously these were only
available in-memory (``SymbolManager.last_tier_changes``) and lost on
restart. The 30-day evaluation framework's tiering component (the dominant
signal in the decision matrix) requires a reliable, timestamped record of
every promotion/demotion to classify.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tier_changes (
            id bigserial PRIMARY KEY,
            symbol_id int NOT NULL REFERENCES symbols (id),
            ts timestamptz NOT NULL,
            from_tier smallint NOT NULL,
            to_tier smallint NOT NULL,
            reason text NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX tier_changes_symbol_ts_idx ON tier_changes (symbol_id, ts DESC)")
    op.execute("CREATE INDEX tier_changes_ts_idx ON tier_changes (ts DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE tier_changes")
