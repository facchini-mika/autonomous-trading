"""order_attempts: durable idempotency / dedup log for place_order.

Revision ID: 0009_order_attempts
Revises: 0008_web_search_call_cost
Create Date: 2026-05-19

The Phase-4 ``InMemoryIdempotencyStore`` (``shared.adapters.polymarket``)
guarantees idempotency only within a single process. A crash between
``POST /order`` and the in-memory ``put`` lets the next cycle re-submit
the same logical order. Polymarket CLOB v2 does not expose a client-side
``clientOrderID`` to deduplicate on the broker side, so dedup has to be
enforced locally and persistently.

This table is the durable backing store for the dedup cache. Each row
represents one logical submission attempt keyed by ``idempotency_key``
(``"<cycle_id>:<decision_id>"`` for live trades). The Lead-driven order
loop ``RESERVE``s the key (``INSERT ... ON CONFLICT DO NOTHING``) before
the HTTP request — a conflict means a prior cycle is still in-flight or
crashed mid-submit, and the adapter refuses to re-fire the order.

The row is updated to its terminal state (``filled`` / ``partial`` /
``rejected`` / ``cancelled`` / ``error``) once the CLOB response (or a
final transient failure) is in hand. Pending rows older than
``ORPHAN_ATTEMPT_WARN_AFTER_MIN`` are surfaced by the Lead at next
cycle start and exported as ``orphan_order_attempts_total`` so the
operator can reconcile manually.

The table is *not* a trade ledger — ``trades`` / ``paper_trades`` keep
that role and are written only on a successful fill. ``order_attempts``
is the audit / dedup log of the *submission attempt itself*.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_order_attempts"
down_revision: str | Sequence[str] | None = "0008_web_search_call_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_attempts",
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("cycle_id", sa.Text(), nullable=False),
        sa.Column("decision_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.Text(), nullable=True),
        sa.Column("fill_price", sa.Numeric(10, 6), nullable=True),
        sa.Column("filled_size", sa.Numeric(18, 6), nullable=True),
        sa.Column("fees", sa.Numeric(14, 6), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_order_attempts_cycle_id",
        "order_attempts",
        ["cycle_id"],
    )
    op.create_index(
        "ix_order_attempts_status_created",
        "order_attempts",
        ["status", "created_at"],
    )

    op.execute("GRANT INSERT, SELECT, UPDATE ON order_attempts TO trading_cycle")


def downgrade() -> None:
    op.execute("REVOKE ALL ON order_attempts FROM trading_cycle")
    op.drop_index("ix_order_attempts_status_created", table_name="order_attempts")
    op.drop_index("ix_order_attempts_cycle_id", table_name="order_attempts")
    op.drop_table("order_attempts")
