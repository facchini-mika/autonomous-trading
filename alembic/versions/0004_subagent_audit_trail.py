"""subagent + web_search audit trail.

Revision ID: 0004_subagent_audit_trail
Revises: 0003_grants_for_cycle_writes
Create Date: 2026-05-04

Adds two append-only audit tables that capture what was previously discarded
by ``execution.subagent_runner`` and ``research.skills.web_search``:

1. ``subagent_runs`` — one row per ``run_subagent`` call (scanner-reviewer,
   trading-agent, risk-execution). Stores task payload, raw stdout, parsed
   envelope, prompt SHA, latency, cost, and error class on failure paths.
   Lets us retroactively answer "what did the agent see and what did it
   actually return?" without re-running the cycle.

2. ``web_search_calls`` — one row per OpenAI Responses-API call from the
   ``mcp__research__web_search`` tool. Stores query, summary, hits, and
   error type. Joined to ``subagent_runs`` via the loose ``run_id`` key
   the runner propagates through the subprocess environment.

Both tables are write-only for cycle-time roles and SELECT-only for the
analysis paths. Cost on ``web_search_calls`` is deferred — the OpenAI
Responses GA schema does not return per-call cost.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_subagent_audit_trail"
down_revision: str | Sequence[str] | None = "0003_grants_for_cycle_writes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subagent_runs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("cycle_id", sa.Text(), nullable=False),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column(
            "run_id",
            sa.dialects.postgresql.UUID(),
            nullable=False,
            unique=True,
        ),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("prompt_sha", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("usage", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("task_payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("raw_stdout", sa.Text(), nullable=True),
        sa.Column("envelope", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_subagent_runs_cycle_agent",
        "subagent_runs",
        ["cycle_id", "agent_name"],
    )

    op.create_table(
        "web_search_calls",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("run_id", sa.dialects.postgresql.UUID(), nullable=True),
        sa.Column("cycle_id", sa.Text(), nullable=True),
        sa.Column("agent_name", sa.Text(), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("hits", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column("elapsed_sec", sa.Numeric(8, 3), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_web_search_calls_cycle_started",
        "web_search_calls",
        ["cycle_id", sa.text("started_at DESC")],
    )
    op.create_index("ix_web_search_calls_run", "web_search_calls", ["run_id"])

    # trading_cycle writes both tables (subagent_runner runs in-process,
    # web_search.py runs in the same subprocess as the trading-agent and
    # uses trading_cycle credentials).
    op.execute("GRANT INSERT, SELECT ON subagent_runs TO trading_cycle")
    op.execute("GRANT INSERT, SELECT ON web_search_calls TO trading_cycle")


def downgrade() -> None:
    op.execute("REVOKE ALL ON web_search_calls FROM trading_cycle")
    op.execute("REVOKE ALL ON subagent_runs FROM trading_cycle")
    op.drop_index("ix_web_search_calls_run", table_name="web_search_calls")
    op.drop_index("ix_web_search_calls_cycle_started", table_name="web_search_calls")
    op.drop_table("web_search_calls")
    op.drop_index("ix_subagent_runs_cycle_agent", table_name="subagent_runs")
    op.drop_table("subagent_runs")
