"""web_search_calls token + cost columns.

Revision ID: 0008_web_search_call_cost
Revises: 0007_predictions_cycle_id
Create Date: 2026-05-05

The original ``web_search_calls`` table (revision 0004) deferred per-call
cost because the OpenAI Responses GA schema did not return it. Token usage
*is* available on ``response.usage`` and the number of internal web searches
can be counted from ``response.output`` items of type ``web_search_call``.

This revision adds the columns needed to compute and store per-call cost
in USD so the per-cycle cost-report CLI can aggregate them. All columns are
nullable; rows written before this revision keep NULL values, and rows
written for failed calls (``error_type IS NOT NULL``) also keep NULL — we
only persist costs for successful calls where ``response.usage`` is real.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_web_search_call_cost"
down_revision: str | Sequence[str] | None = "0007_predictions_cycle_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "web_search_calls",
        sa.Column("input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "web_search_calls",
        sa.Column("output_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "web_search_calls",
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "web_search_calls",
        sa.Column("web_search_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "web_search_calls",
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
    )
    op.create_index(
        "ix_web_search_calls_cycle_cost",
        "web_search_calls",
        ["cycle_id", "cost_usd"],
    )


def downgrade() -> None:
    op.drop_index("ix_web_search_calls_cycle_cost", table_name="web_search_calls")
    op.drop_column("web_search_calls", "cost_usd")
    op.drop_column("web_search_calls", "web_search_count")
    op.drop_column("web_search_calls", "cached_input_tokens")
    op.drop_column("web_search_calls", "output_tokens")
    op.drop_column("web_search_calls", "input_tokens")
