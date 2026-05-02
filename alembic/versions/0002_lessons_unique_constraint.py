"""Idempotency: partial unique index on lessons (source_agent_id, trigger_event_id).

Revision ID: 0002_lessons_unique_constraint
Revises: 0001_initial_schema
Create Date: 2026-05-02

Phase 4c (lessons_summary) re-runs are idempotent at the (source_agent_id,
trigger_event_id) granularity. Without a unique index, a daily cron retry
after a partial failure would emit duplicate lesson rows. The index is
partial because some lessons (manually authored, no trigger event) carry
NULL trigger_event_id and must be allowed to coexist.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_lessons_unique_constraint"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_NAME = "uq_lessons_source_trigger"


def upgrade() -> None:
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
        "ON lessons (source_agent_id, trigger_event_id) "
        "WHERE trigger_event_id IS NOT NULL",
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
