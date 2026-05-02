"""Phase-5 GRANT fixes uncovered by the E2E paper-cycle test.

Revision ID: 0003_grants_for_cycle_writes
Revises: 0002_lessons_unique_constraint
Create Date: 2026-05-02

The Phase-3 GRANT matrix omitted two privileges that the cron flow needs:

1. ``trading_cycle`` had SELECT on ``markets`` but not INSERT/UPDATE. The Lead
   upserts the universe of markets observed by ``scanner-reviewer`` before any
   FK-dependent insert (predictions, decisions, paper_trades all FK to markets);
   without INSERT we hit ``InsufficientPrivilege`` on the first cycle of a clean
   database.

2. ``outcome_ingestion`` and ``lessons_summary`` write a high-water-mark row to
   ``system_state`` via ``INSERT ... ON CONFLICT DO UPDATE``. Postgres requires
   INSERT privilege on the table for the INSERT half of that statement, even if
   the row already exists. Without it the very first cron run aborts.

Both gaps are additive grants — no schema changes, no data migration. Downgrade
revokes only the two new privileges and leaves the Phase-3 matrix intact.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_grants_for_cycle_writes"
down_revision: str | Sequence[str] | None = "0002_lessons_unique_constraint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT INSERT, UPDATE ON markets TO trading_cycle")
    op.execute("GRANT INSERT ON system_state TO outcome_ingestion, lessons_summary")


def downgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE ON markets FROM trading_cycle")
    op.execute("REVOKE INSERT ON system_state FROM outcome_ingestion, lessons_summary")
