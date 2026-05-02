"""Daily lesson-summary script (deterministic, no LLM).

Selects resolved predictions in the lookback window, filters via the surprise
heuristic, and INSERTs new rows into `lessons` keyed by
`(source_agent_id, trigger_event_id)`. Re-runs are idempotent thanks to the
partial unique index added in migration 0002.

Authority boundary: binds to the `lessons_summary` Postgres role (SELECT +
INSERT on lessons + UPDATE on system_state.value/updated_at only).

Run via:
    uv run python -m research.skills.lessons_summary
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from research.lessons_template import action_taken, hypothesis_for, lesson_body
from research.surprise_heuristic import categorize, is_surprise
from shared.config.settings import Settings
from shared.db import get_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

HIGH_WATER_MARK_KEY: Final = "last_lessons_summary_at"


def run_once(
    *,
    settings: Settings | None = None,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """Generate lessons for surprises in the lookback window.

    Returns the number of new lesson rows inserted.
    """
    cfg = settings or Settings()
    factory = session_factory or _default_factory
    now = (clock or _utcnow)()
    cutoff = now - timedelta(days=cfg.LESSONS_LOOKBACK_DAYS)

    inserted = 0
    with factory() as session:
        rows = session.execute(
            text(
                """
                SELECT p.id, p.market_id, p.agent_id, p.p_raw, p.outcome,
                       p.realized_pnl, p.created_at,
                       d.cycle_id, d.edge, d.action, t.notional_usd
                FROM predictions p
                LEFT JOIN decisions d
                    ON d.market_id = p.market_id AND d.cycle_id IS NOT NULL
                LEFT JOIN trades t ON t.decision_id = d.id
                WHERE p.outcome IS NOT NULL
                  AND p.created_at >= :cutoff
                ORDER BY p.created_at DESC
                """,
            ),
            {"cutoff": cutoff},
        ).all()

        for row in rows:
            p_yes = float(row.p_raw)
            outcome_yes = bool(row.outcome)
            realized = float(row.realized_pnl) if row.realized_pnl is not None else None
            notional = float(row.notional_usd) if row.notional_usd is not None else 0.0
            edge = float(row.edge) if row.edge is not None else (p_yes - 0.5)
            if not is_surprise(
                p_yes=p_yes,
                outcome_yes=outcome_yes,
                realized_pnl=realized,
                notional=notional,
                edge=edge,
                threshold=cfg.SURPRISE_THRESHOLD,
            ):
                continue
            category = categorize(p_yes, outcome_yes=outcome_yes, threshold=cfg.SURPRISE_THRESHOLD)
            body = lesson_body(
                category=category,
                market_id=str(row.market_id),
                p_yes=p_yes,
                outcome_yes=outcome_yes,
                realized_pnl=realized,
                notional=notional,
                edge=edge,
                cycle_id=str(row.cycle_id) if row.cycle_id else None,
            )
            try:
                session.execute(
                    text(
                        """
                        INSERT INTO lessons (
                            source_agent_id, trigger_event_id, market_id,
                            observation, hypothesis, action_taken, outcome,
                            status, tags, created_at
                        )
                        VALUES (
                            :agent_id, :trigger, :market_id,
                            :body, :hypothesis, :action_taken, :outcome,
                            'open', ARRAY[:category], NOW()
                        )
                        """,
                    ),
                    {
                        "agent_id": str(row.agent_id),
                        "trigger": str(row.id),
                        "market_id": str(row.market_id),
                        "body": body,
                        "hypothesis": hypothesis_for(category),
                        "action_taken": action_taken(realized),
                        "outcome": "YES" if outcome_yes else "NO",
                        "category": category,
                    },
                )
                inserted += 1
            except IntegrityError as exc:
                logger.info("Lesson already exists for prediction %s; skipping (%s)", row.id, exc)
                session.rollback()

    _write_high_water_mark(factory, now)
    logger.info("lessons_summary done: %d lessons inserted", inserted)
    return inserted


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("lessons_summary")


def _write_high_water_mark(
    factory: Callable[[], AbstractContextManager[Session]],
    ts: datetime,
) -> None:
    with factory() as session:
        session.execute(
            text(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (:k, jsonb_build_object('ts', :v), NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """,
            ),
            {"k": HIGH_WATER_MARK_KEY, "v": ts.isoformat()},
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_once()
