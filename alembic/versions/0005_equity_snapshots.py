"""equity_snapshots: per-cycle PnL/exposure/duration row.

Revision ID: 0005_equity_snapshots
Revises: 0004_subagent_audit_trail
Create Date: 2026-05-05

Backs the Prometheus exporter (P1.1, ``data_infrastructure.md §3``). One row
per cycle is appended at cycle-end with:

* ``equity_usd``         — ``PortfolioState.equity`` at cycle close
* ``peak_equity_usd``    — running maximum across all snapshots, used for
                            the ``drawdown_pct`` gauge
* ``gross_exposure_usd`` — passed through from ``PortfolioState``
* ``duration_seconds``   — wall-clock cycle latency, fed into the
                            ``cycle_duration_seconds`` histogram

The exporter scrapes via ``trading_cycle`` role (read-only against this
table for the daemon process; the ``Lead`` writes via the same role at
cycle-end). Adding a dedicated ``metrics_exporter`` role is deferred —
this table only joins existing ``trading_cycle``-readable tables, so
least-privilege can be tightened in a follow-up without schema changes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_equity_snapshots"
down_revision: str | Sequence[str] | None = "0004_subagent_audit_trail"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "equity_snapshots",
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("cycle_id", sa.Text(), nullable=False),
        sa.Column("equity_usd", sa.Numeric(14, 2), nullable=False),
        sa.Column("peak_equity_usd", sa.Numeric(14, 2), nullable=False),
        sa.Column("gross_exposure_usd", sa.Numeric(14, 2), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(10, 3), nullable=False),
        sa.PrimaryKeyConstraint("time", "cycle_id"),
    )
    op.create_index(
        "ix_equity_snapshots_time",
        "equity_snapshots",
        [sa.text("time DESC")],
    )

    op.execute("GRANT INSERT, SELECT ON equity_snapshots TO trading_cycle")
    op.execute("GRANT SELECT ON equity_snapshots TO outcome_ingestion, lessons_summary")


def downgrade() -> None:
    op.execute("REVOKE ALL ON equity_snapshots FROM trading_cycle, outcome_ingestion, lessons_summary")
    op.drop_index("ix_equity_snapshots_time", table_name="equity_snapshots")
    op.drop_table("equity_snapshots")
