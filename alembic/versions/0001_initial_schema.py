"""initial schema: 11 tables + column-level GRANTs.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-02

Creates the canonical schema (markets, market_snapshots, predictions,
decisions, trades, paper_trades, positions, notes, cycle_plan, lessons,
system_state) and grants the three app roles (trading_cycle,
outcome_ingestion, lessons_summary) the per-column privileges defined in
the role-matrix in `specs/engineering.md` and `specs/data_infrastructure.md`.

Roles themselves are NOT created here — they are infrastructure managed by
`infra/sql/00_roles.sql` (Docker-init mount). Downgrade revokes the GRANTs
and drops all tables; roles persist across downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


APP_ROLES = ("trading_cycle", "outcome_ingestion", "lessons_summary")


def _ensure_pgcrypto() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def _create_tables() -> None:
    op.create_table(
        "markets",
        sa.Column("market_id", sa.Text(), primary_key=True),
        sa.Column("condition_id", sa.Text(), nullable=False, unique=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("end_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("resolution_source", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_seen", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ambiguity_score", sa.Numeric(4, 3), nullable=True),
    )
    op.create_index("ix_markets_status", "markets", ["status"])
    op.create_index("ix_markets_end_date", "markets", ["end_date"])
    op.create_index("ix_markets_last_seen", "markets", ["last_seen"])

    op.create_table(
        "market_snapshots",
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("market_id", sa.Text(), sa.ForeignKey("markets.market_id"), nullable=False),
        sa.Column("best_bid", sa.Numeric(5, 4), nullable=False),
        sa.Column("best_ask", sa.Numeric(5, 4), nullable=False),
        sa.Column("mid", sa.Numeric(5, 4), nullable=True),
        sa.Column("depth_bid_1pct", sa.Numeric(12, 2), nullable=True),
        sa.Column("depth_ask_1pct", sa.Numeric(12, 2), nullable=True),
        sa.Column("volume_24h", sa.Numeric(12, 2), nullable=True),
        sa.PrimaryKeyConstraint("time", "market_id"),
    )
    op.create_index("ix_market_snapshots_market_time", "market_snapshots", ["market_id", sa.text("time DESC")])

    op.create_table(
        "predictions",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("market_id", sa.Text(), sa.ForeignKey("markets.market_id"), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("p_raw", sa.Numeric(5, 4), nullable=False),
        sa.Column("inference_log", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.Boolean(), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_predictions_market_created", "predictions", ["market_id", sa.text("created_at DESC")])
    op.create_index("ix_predictions_agent_created", "predictions", ["agent_id", sa.text("created_at DESC")])
    op.create_index(
        "ix_predictions_outcome_null", "predictions", ["outcome"], postgresql_where=sa.text("outcome IS NULL")
    )

    op.create_table(
        "decisions",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cycle_id", sa.Text(), nullable=False),
        sa.Column("market_id", sa.Text(), sa.ForeignKey("markets.market_id"), nullable=False),
        sa.Column("p_consensus", sa.Numeric(5, 4), nullable=False),
        sa.Column("q_market", sa.Numeric(5, 4), nullable=False),
        sa.Column("edge", sa.Numeric(5, 4), nullable=True),
        sa.Column("gate_results", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_decisions_cycle", "decisions", ["cycle_id"])
    op.create_index("ix_decisions_market_created", "decisions", ["market_id", sa.text("created_at DESC")])
    op.create_index("ix_decisions_action", "decisions", ["action"])

    for table_name in ("trades", "paper_trades"):
        op.create_table(
            table_name,
            sa.Column(
                "id", sa.dialects.postgresql.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")
            ),
            sa.Column("decision_id", sa.dialects.postgresql.UUID(), sa.ForeignKey("decisions.id"), nullable=False),
            sa.Column("market_id", sa.Text(), sa.ForeignKey("markets.market_id"), nullable=False),
            sa.Column("side", sa.Text(), nullable=False),
            sa.Column("size", sa.Numeric(12, 4), nullable=False),
            sa.Column("price", sa.Numeric(5, 4), nullable=False),
            sa.Column("notional_usd", sa.Numeric(14, 2), nullable=False),
            sa.Column("fees", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("gas", sa.Numeric(12, 2), nullable=False, server_default="0"),
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
            sa.Column("broker_order_id", sa.Text(), nullable=True, unique=True),
            sa.Column("parent_trade_id", sa.dialects.postgresql.UUID(), nullable=True),
            sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        )
        op.create_index(f"ix_{table_name}_decision", table_name, ["decision_id"])
        op.create_index(f"ix_{table_name}_market", table_name, ["market_id"])
        op.create_index(f"ix_{table_name}_status", table_name, ["status"])
        op.create_index(f"ix_{table_name}_created", table_name, [sa.text("created_at DESC")])

    op.create_table(
        "positions",
        sa.Column("market_id", sa.Text(), sa.ForeignKey("markets.market_id"), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("size", sa.Numeric(12, 4), nullable=False),
        sa.Column("avg_price", sa.Numeric(5, 4), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(14, 2), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_updated", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.PrimaryKeyConstraint("market_id", "side"),
    )
    op.create_index("ix_positions_status", "positions", ["status"])
    op.create_index("ix_positions_last_updated", "positions", ["last_updated"])

    op.create_table(
        "notes",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("tags", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("last_accessed", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_notes_agent_lru", "notes", ["agent_id", sa.text("last_accessed DESC")])

    op.create_table(
        "cycle_plan",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("written_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("written_by_cycle_id", sa.Text(), nullable=False),
        sa.Column(
            "next_priorities", sa.dialects.postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("holds_with_rationale", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("pending_settlements", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("opportunities_deferred", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("blockers", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_cycle_plan_active",
        "cycle_plan",
        [sa.text("written_at DESC")],
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    op.create_table(
        "lessons",
        sa.Column("id", sa.dialects.postgresql.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_agent_id", sa.Text(), nullable=False),
        sa.Column("trigger_event_id", sa.dialects.postgresql.UUID(), nullable=True),
        sa.Column("market_id", sa.Text(), nullable=True),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=True),
        sa.Column("action_taken", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("parent_pattern_id", sa.dialects.postgresql.UUID(), nullable=True),
        sa.Column("tags", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_lessons_agent_created", "lessons", ["source_agent_id", sa.text("created_at DESC")])
    op.create_index("ix_lessons_status", "lessons", ["status"])

    op.create_table(
        "system_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )


def _grant_role_privileges() -> None:
    """Apply the column-level GRANT matrix for the three app roles."""
    # trading_cycle: read everything; write to most tables; lessons read-only.
    op.execute("""
        GRANT SELECT ON markets, market_snapshots, predictions, decisions,
            trades, paper_trades, positions, notes, cycle_plan, lessons,
            system_state TO trading_cycle;
        GRANT INSERT, UPDATE ON market_snapshots, predictions, decisions,
            trades, paper_trades, positions, notes, cycle_plan TO trading_cycle;
        GRANT UPDATE ON system_state TO trading_cycle;
    """)

    # outcome_ingestion: read all; narrow column-level UPDATEs only.
    op.execute("""
        GRANT SELECT ON markets, market_snapshots, predictions, decisions,
            trades, paper_trades, positions, system_state TO outcome_ingestion;
        GRANT UPDATE (outcome, realized_pnl) ON predictions TO outcome_ingestion;
        GRANT UPDATE (status, realized_pnl) ON trades TO outcome_ingestion;
        GRANT UPDATE (status, realized_pnl) ON paper_trades TO outcome_ingestion;
        GRANT UPDATE (status, realized_pnl, last_updated) ON positions TO outcome_ingestion;
        GRANT UPDATE (value, updated_at) ON system_state TO outcome_ingestion;
    """)

    # lessons_summary: read most; INSERT-only on lessons; narrow UPDATE on system_state.
    op.execute("""
        GRANT SELECT ON markets, predictions, decisions, trades, paper_trades,
            positions, lessons, system_state TO lessons_summary;
        GRANT INSERT ON lessons TO lessons_summary;
        GRANT UPDATE (value, updated_at) ON system_state TO lessons_summary;
    """)


def _revoke_role_privileges() -> None:
    """Mirror of _grant_role_privileges for downgrade."""
    op.execute("""
        REVOKE ALL ON markets, market_snapshots, predictions, decisions,
            trades, paper_trades, positions, notes, cycle_plan, lessons,
            system_state FROM trading_cycle, outcome_ingestion, lessons_summary;
    """)


def upgrade() -> None:
    _ensure_pgcrypto()
    _create_tables()
    _grant_role_privileges()


def downgrade() -> None:
    _revoke_role_privileges()
    for table in (
        "system_state",
        "lessons",
        "cycle_plan",
        "notes",
        "positions",
        "paper_trades",
        "trades",
        "decisions",
        "predictions",
        "market_snapshots",
        "markets",
    ):
        op.drop_table(table)
