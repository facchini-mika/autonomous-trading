"""agent_performance: per-agent rolling Tier-1 stats.

Revision ID: 0006_agent_performance
Revises: 0005_equity_snapshots
Create Date: 2026-05-05

Backs the Tier-1 Trade Evaluation Team described in
``specs/trading_feedback.md §6 "Weiterer Ausbau"``. Schema follows
``data_infrastructure.md §1``:

    agent_performance(agent_id, time, hit_rate_30d, sharpe_30d, pnl_30d, n_samples)

Writer role: ``lessons_summary``. Spec §6 sketches a dedicated
``trade_evaluator`` role for the post-MVP Tier-1 team, but ``lessons_summary``
already carries SELECT on every source table the Tier-1 orchestrator needs
(predictions, decisions, trades, paper_trades, positions, system_state per
migration 0001) and is the closest semantic fit (computed periodic
summaries). Adding a fourth role would have meant extending
``infra/sql/00_roles.sh``, the docker-compose env list, and the CI workflow;
the latter would in turn fail the ``review`` gate per the workflow-self-edit
OIDC pitfall. Tightening to a least-privilege Tier-1 role is a follow-up
when capital scales.

Reads are open to ``trading_cycle`` so the Prometheus collector can scrape
the latest snapshot per agent, and to ``outcome_ingestion`` for defensive
cross-reference in future revisions.

Idempotency: every successful Tier-1 cycle inserts ONE row per agent_id
keyed on ``(agent_id, time)``; the orchestrator stamps ``time`` from
``datetime.now(UTC)`` at write time, so consecutive runs always carry
distinct timestamps.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_agent_performance"
down_revision: str | Sequence[str] | None = "0005_equity_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_performance",
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("hit_rate_30d", sa.Numeric(6, 4), nullable=True),
        sa.Column("sharpe_30d", sa.Numeric(8, 4), nullable=True),
        sa.Column("pnl_30d", sa.Numeric(14, 2), nullable=False),
        sa.Column("n_samples", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.PrimaryKeyConstraint("agent_id", "time"),
    )
    op.create_index(
        "ix_agent_performance_agent_time",
        "agent_performance",
        ["agent_id", sa.text("time DESC")],
    )

    # Writer role: lessons_summary already carries SELECT on every source
    # table the orchestrator reads. We add INSERT/SELECT on the new table.
    op.execute("GRANT INSERT, SELECT ON agent_performance TO lessons_summary")

    # Read access for downstream consumers.
    op.execute(
        "GRANT SELECT ON agent_performance TO trading_cycle, outcome_ingestion",
    )


def downgrade() -> None:
    op.execute(
        "REVOKE ALL ON agent_performance FROM trading_cycle, lessons_summary, outcome_ingestion",
    )
    op.drop_index("ix_agent_performance_agent_time", table_name="agent_performance")
    op.drop_table("agent_performance")
