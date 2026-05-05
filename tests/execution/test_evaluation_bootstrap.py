"""Unit tests for ``execution.evaluation_bootstrap``.

Three deterministic-fake subagent callables are injected; the orchestrator
plus DB layer is exercised against an in-memory FakeSession that captures
INSERT/UPDATE statements for assertion.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest

from execution.evaluation_bootstrap import (
    EvaluationAbortedError,
    bootstrap_evaluation_team,
)
from execution.subagent_runner import SubagentBudgetError
from shared.config.settings import Settings
from shared.models import (
    AgentPerformanceOutput,
    AgentPerformanceRow,
    AgentPerformanceTask,
    OutcomeFetcherOutput,
    OutcomeFetcherTask,
    PerAgentPnL,
    PnlAggregatorOutput,
    PnlAggregatorTask,
    ResolvedPrediction,
)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Records INSERT/UPDATE statements; returns canned SELECT rows."""

    def __init__(self, select_responses: list[tuple[str, list[Any]]]) -> None:
        self._select_responses = select_responses
        self.executions: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: object, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = " ".join(str(query).split())
        self.executions.append((sql, params or {}))
        if "SELECT" in sql.upper() and "INSERT" not in sql.upper():
            for key, rows in self._select_responses:
                if key in sql:
                    return _FakeResult(rows)
        return _FakeResult([])


@contextmanager
def _ctx(session: _FakeSession) -> Iterator[_FakeSession]:
    yield session


def _factory(session: _FakeSession) -> Any:
    return cast("Any", lambda: _ctx(session))


def _resolved(agent_id: str = "trading-agent") -> ResolvedPrediction:
    return ResolvedPrediction(
        prediction_id=uuid4(),
        market_id="m1",
        agent_id=agent_id,
        p_raw=0.6,
        outcome=True,
        realized_pnl=0.5,
        resolved_at=datetime.now(UTC),
    )


def _ok_outcome_fetcher(now: datetime) -> Any:
    def _impl(task: OutcomeFetcherTask) -> OutcomeFetcherOutput:
        return OutcomeFetcherOutput(
            resolved_predictions=task.candidate_predictions,
            next_high_water_mark=now,
        )

    return _impl


def _ok_pnl_aggregator() -> Any:
    def _impl(task: PnlAggregatorTask) -> PnlAggregatorOutput:
        agents = sorted({rp.agent_id for rp in task.resolved_predictions})
        by_agent = []
        for agent_id in agents:
            group = [rp for rp in task.resolved_predictions if rp.agent_id == agent_id]
            by_agent.append(
                PerAgentPnL(
                    agent_id=agent_id,
                    realized_pnl_30d=sum(rp.realized_pnl or 0.0 for rp in group),
                    n_resolved_30d=len(group),
                    n_wins_30d=sum(1 for rp in group if rp.outcome and rp.p_raw > 0.5),
                    pnl_per_trade=[rp.realized_pnl for rp in group if rp.realized_pnl is not None],
                ),
            )
        return PnlAggregatorOutput(by_agent=by_agent)

    return _impl


def _ok_perf_updater() -> Any:
    def _impl(task: AgentPerformanceTask) -> AgentPerformanceOutput:
        rows = []
        for a in task.by_agent:
            hit = (a.n_wins_30d / a.n_resolved_30d) if a.n_resolved_30d else None
            rows.append(
                AgentPerformanceRow(
                    agent_id=a.agent_id,
                    hit_rate_30d=hit,
                    sharpe_30d=None,
                    pnl_30d=a.realized_pnl_30d,
                    n_samples=a.n_resolved_30d,
                ),
            )
        return AgentPerformanceOutput(rows=rows)

    return _impl


def test_orchestrator_persists_one_row_per_agent_and_bumps_hwm() -> None:
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    pred = _PredRow(
        agent_id="trading-agent",
        p_raw=0.6,
        outcome=True,
        pnl=0.5,
        when=now - timedelta(days=1),
    )
    session = _FakeSession(
        [
            ("FROM system_state", []),
            ("FROM predictions", [pred]),
        ],
    )
    artifacts = bootstrap_evaluation_team(
        settings=Settings(),
        outcome_fetcher=_ok_outcome_fetcher(now),
        pnl_aggregator=_ok_pnl_aggregator(),
        perf_updater=_ok_perf_updater(),
        session_factory=_factory(session),
        clock=lambda: now,
    )
    assert artifacts.n_resolved == 1
    assert len(artifacts.rows) == 1
    assert artifacts.rows[0].agent_id == "trading-agent"
    assert artifacts.rows[0].pnl_30d == 0.5
    assert artifacts.rows[0].hit_rate_30d == 1.0
    assert artifacts.next_high_water_mark == now

    inserts = [sql for sql, _ in session.executions if "INSERT INTO agent_performance" in sql]
    assert len(inserts) == 1
    hwm_writes = [sql for sql, _ in session.executions if "INSERT INTO system_state" in sql]
    assert len(hwm_writes) == 1


def test_no_candidates_skips_inserts_but_still_advances_hwm() -> None:
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    session = _FakeSession([("FROM system_state", []), ("FROM predictions", [])])
    artifacts = bootstrap_evaluation_team(
        settings=Settings(),
        outcome_fetcher=_ok_outcome_fetcher(now),
        pnl_aggregator=_ok_pnl_aggregator(),
        perf_updater=_ok_perf_updater(),
        session_factory=_factory(session),
        clock=lambda: now,
    )
    assert artifacts.n_resolved == 0
    assert artifacts.rows == []
    assert not [sql for sql, _ in session.executions if "INSERT INTO agent_performance" in sql]
    # HWM still bumped so the next run starts from `now`.
    hwm_writes = [sql for sql, _ in session.executions if "INSERT INTO system_state" in sql]
    assert len(hwm_writes) == 1


def test_subagent_budget_error_aborts_with_stage() -> None:
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    session = _FakeSession([("FROM system_state", []), ("FROM predictions", [])])

    def failing(_task: OutcomeFetcherTask) -> OutcomeFetcherOutput:
        raise SubagentBudgetError("out of credits")

    with pytest.raises(EvaluationAbortedError) as excinfo:
        bootstrap_evaluation_team(
            settings=Settings(),
            outcome_fetcher=cast("Any", failing),
            pnl_aggregator=_ok_pnl_aggregator(),
            perf_updater=_ok_perf_updater(),
            session_factory=_factory(session),
            clock=lambda: now,
        )
    assert excinfo.value.stage == "outcome-fetcher"
    # No agent_performance inserts on abort.
    assert not [sql for sql, _ in session.executions if "INSERT INTO agent_performance" in sql]


def test_high_water_mark_loaded_from_existing_row() -> None:
    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    prev = (now - timedelta(hours=2)).isoformat()
    session = _FakeSession(
        [
            ("FROM system_state", [_HwmRow({"at": prev})]),
            ("FROM predictions", []),
        ],
    )
    captured: list[OutcomeFetcherTask] = []

    def capturing_outcome_fetcher(task: OutcomeFetcherTask) -> OutcomeFetcherOutput:
        captured.append(task)
        return OutcomeFetcherOutput(resolved_predictions=[], next_high_water_mark=now)

    bootstrap_evaluation_team(
        settings=Settings(),
        outcome_fetcher=capturing_outcome_fetcher,
        pnl_aggregator=_ok_pnl_aggregator(),
        perf_updater=_ok_perf_updater(),
        session_factory=_factory(session),
        clock=lambda: now,
    )
    assert captured
    assert captured[0].high_water_mark is not None
    assert captured[0].high_water_mark.isoformat() == prev


# -- helper row stubs --------------------------------------------------------


class _PredRow:
    def __init__(self, *, agent_id: str, p_raw: float, outcome: bool, pnl: float, when: datetime) -> None:
        self.id = uuid4()
        self.market_id = "m1"
        self.agent_id = agent_id
        self.p_raw = p_raw
        self.outcome = outcome
        self.realized_pnl = pnl
        self.resolved_at = when


class _HwmRow:
    def __init__(self, value: dict[str, str]) -> None:
        self.value = value
