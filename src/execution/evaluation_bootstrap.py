"""Lead orchestration for one Tier-1 evaluation cycle.

The evaluation Lead is a deterministic coordinator (no LLM). It loads the
high-water mark and the candidate resolved-prediction batch, dispatches
three sub-tasks (outcome-fetcher → pnl-aggregator → agent-performance-
updater), persists one ``agent_performance`` row per agent, and bumps
``system_state.last_evaluation_at``.

Sub-agent execution is injected as callables so tests can pass synchronous
fakes; production wires them to real Claude Code subagent invocations via
``execution.run_evaluation`` (mirrors the trading-cycle Lead).

Spec: ``trading_feedback.md §6 "Weiterer Ausbau"`` (Trade Evaluation Team).
"""

from __future__ import annotations

import json
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text

from execution.subagent_runner import SubagentBudgetError
from shared.db import get_session
from shared.logging import bind, get_logger, unbind
from shared.models import (
    AgentPerformanceOutput,
    AgentPerformanceRow,
    AgentPerformanceTask,
    OutcomeFetcherOutput,
    OutcomeFetcherTask,
    PnlAggregatorOutput,
    PnlAggregatorTask,
    ResolvedPrediction,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

    from shared.config.settings import Settings

logger = get_logger(__name__)

HIGH_WATER_MARK_KEY = "last_evaluation_at"


@dataclass(frozen=True)
class EvaluationArtifacts:
    """End-of-cycle artifacts the entry point logs / tests assert on."""

    cycle_id: str
    snapshot_time: datetime
    n_candidates: int
    n_resolved: int
    rows: list[AgentPerformanceRow]
    next_high_water_mark: datetime


class EvaluationAbortedError(Exception):
    """Raised when a subagent budget aborts mid-cycle (mirrors CycleAbortedError)."""

    def __init__(self, *, reason: str, cycle_id: str, stage: str) -> None:
        self.reason = reason
        self.cycle_id = cycle_id
        self.stage = stage
        super().__init__(f"evaluation cycle {cycle_id} aborted at {stage}: {reason}")


def bootstrap_evaluation_team(
    *,
    settings: Settings,
    outcome_fetcher: Callable[[OutcomeFetcherTask], OutcomeFetcherOutput],
    pnl_aggregator: Callable[[PnlAggregatorTask], PnlAggregatorOutput],
    perf_updater: Callable[[AgentPerformanceTask], AgentPerformanceOutput],
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> EvaluationArtifacts:
    """Run one Tier-1 evaluation cycle end-to-end.

    Idempotent under re-run: ``agent_performance(agent_id, time)`` is a PK
    and ``snapshot_time`` is set from ``datetime.now(UTC)`` so consecutive
    runs always carry distinct timestamps.
    """
    factory = session_factory or _default_factory
    now = (clock or _utcnow)()
    cycle_id = f"eval-{int(now.timestamp())}"
    bind(cycle_id=cycle_id)
    logger.info("evaluation_starting", lookback_days=settings.EVALUATION_LOOKBACK_DAYS)

    high_water_mark = _load_high_water_mark(factory)
    cutoff = now - timedelta(days=settings.EVALUATION_LOOKBACK_DAYS)
    candidates = _load_resolved_predictions(factory=factory, cutoff=cutoff)
    logger.info(
        "evaluation_candidates_loaded",
        n=len(candidates),
        cutoff=cutoff.isoformat(),
        high_water_mark=high_water_mark.isoformat() if high_water_mark else None,
    )

    try:
        outcome_out = outcome_fetcher(
            OutcomeFetcherTask(
                cycle_id=cycle_id,
                cycle_clock=now.isoformat(),
                high_water_mark=high_water_mark,
                lookback_days=settings.EVALUATION_LOOKBACK_DAYS,
                candidate_predictions=candidates,
            ),
        )
    except SubagentBudgetError as exc:
        unbind("cycle_id")
        raise EvaluationAbortedError(reason=str(exc), cycle_id=cycle_id, stage="outcome-fetcher") from exc

    try:
        pnl_out = pnl_aggregator(
            PnlAggregatorTask(
                cycle_id=cycle_id,
                resolved_predictions=outcome_out.resolved_predictions,
            ),
        )
    except SubagentBudgetError as exc:
        unbind("cycle_id")
        raise EvaluationAbortedError(reason=str(exc), cycle_id=cycle_id, stage="pnl-aggregator") from exc

    try:
        perf_out = perf_updater(
            AgentPerformanceTask(
                cycle_id=cycle_id,
                snapshot_time=now,
                by_agent=pnl_out.by_agent,
            ),
        )
    except SubagentBudgetError as exc:
        unbind("cycle_id")
        raise EvaluationAbortedError(
            reason=str(exc),
            cycle_id=cycle_id,
            stage="agent-performance-updater",
        ) from exc

    rows = list(perf_out.rows)
    _persist_rows(factory=factory, snapshot_time=now, rows=rows)
    _persist_high_water_mark(factory=factory, value=outcome_out.next_high_water_mark)

    logger.info(
        "evaluation_done",
        cycle_id=cycle_id,
        n_resolved=len(outcome_out.resolved_predictions),
        n_rows=len(rows),
    )
    unbind("cycle_id")

    return EvaluationArtifacts(
        cycle_id=cycle_id,
        snapshot_time=now,
        n_candidates=len(candidates),
        n_resolved=len(outcome_out.resolved_predictions),
        rows=rows,
        next_high_water_mark=outcome_out.next_high_water_mark,
    )


# -- helpers -----------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("lessons_summary")


def _load_high_water_mark(
    factory: Callable[[], AbstractContextManager[Session]],
) -> datetime | None:
    """Return the timestamp from system_state.last_evaluation_at, or None."""
    with factory() as session:
        row = session.execute(
            text("SELECT value FROM system_state WHERE key = :key"),
            {"key": HIGH_WATER_MARK_KEY},
        ).first()
    if row is None:
        return None
    value = row.value
    iso: str | None = None
    if isinstance(value, str):
        iso = value
    elif isinstance(value, dict):
        candidate = value.get("at") or value.get("value")
        if isinstance(candidate, str):
            iso = candidate
    if iso is None:
        return None
    parsed = datetime.fromisoformat(iso)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _load_resolved_predictions(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    cutoff: datetime,
) -> list[ResolvedPrediction]:
    """Pull every ``predictions`` row inside the lookback window with outcome set."""
    with factory() as session:
        rows = session.execute(
            text(
                """
                SELECT id, market_id, agent_id, p_raw, outcome,
                       realized_pnl, COALESCE(time, created_at) AS resolved_at
                FROM predictions
                WHERE outcome IS NOT NULL
                  AND COALESCE(time, created_at) >= :cutoff
                ORDER BY agent_id, COALESCE(time, created_at) ASC
                """,
            ),
            {"cutoff": cutoff},
        ).all()
    return [
        ResolvedPrediction(
            prediction_id=r.id,
            market_id=r.market_id,
            agent_id=r.agent_id,
            p_raw=float(r.p_raw),
            outcome=bool(r.outcome),
            realized_pnl=float(r.realized_pnl) if r.realized_pnl is not None else None,
            resolved_at=r.resolved_at,
        )
        for r in rows
    ]


def _persist_rows(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    snapshot_time: datetime,
    rows: list[AgentPerformanceRow],
) -> None:
    """Insert one row per agent. Idempotent on the (agent_id, time) PK."""
    if not rows:
        return
    with factory() as session:
        for row in rows:
            session.execute(
                text(
                    """
                    INSERT INTO agent_performance
                        (agent_id, time, hit_rate_30d, sharpe_30d, pnl_30d, n_samples)
                    VALUES
                        (:agent_id, :time, :hit_rate, :sharpe, :pnl, :n)
                    """,
                ),
                {
                    "agent_id": row.agent_id,
                    "time": snapshot_time,
                    "hit_rate": row.hit_rate_30d,
                    "sharpe": row.sharpe_30d,
                    "pnl": row.pnl_30d,
                    "n": row.n_samples,
                },
            )


def _persist_high_water_mark(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    value: datetime,
) -> None:
    """Store the new high-water mark in system_state."""
    payload = json.dumps({"at": value.isoformat()})
    with factory() as session:
        session.execute(
            text(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (:key, CAST(:value AS jsonb), NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = EXCLUDED.updated_at
                """,
            ),
            {"key": HIGH_WATER_MARK_KEY, "value": payload},
        )
