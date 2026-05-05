"""predictions.cycle_id for prediction-trade aggregation.

Revision ID: 0007_predictions_cycle_id
Revises: 0006_agent_performance
Create Date: 2026-05-05

Adds a ``cycle_id`` column on ``predictions`` so the outcome-ingestion
script can aggregate ``SUM(trades.realized_pnl)`` per prediction via the
join key ``(cycle_id, market_id)``. Without this, predictions had no
way to identify which trades originated from them: the existing schema
links ``trades.decision_id -> decisions.id`` and ``decisions.cycle_id``,
but ``predictions`` carried only ``market_id`` + ``agent_id``, leaving
multi-cycle markets ambiguous.

Nullable for backwards compatibility with rows written before this
revision; the outcome-ingestion aggregator skips rows where cycle_id
IS NULL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_predictions_cycle_id"
down_revision: str | Sequence[str] | None = "0006_agent_performance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "predictions",
        sa.Column("cycle_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_predictions_cycle_market",
        "predictions",
        ["cycle_id", "market_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_predictions_cycle_market", table_name="predictions")
    op.drop_column("predictions", "cycle_id")
