"""Round-trip + validation tests for the Tier-1 evaluation Pydantic models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.models import (
    AgentPerformance,
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


def _resolved() -> ResolvedPrediction:
    return ResolvedPrediction(
        prediction_id=uuid4(),
        market_id="m1",
        agent_id="trading-agent",
        p_raw=0.62,
        outcome=True,
        realized_pnl=0.18,
        resolved_at=datetime.now(UTC),
    )


def test_agent_performance_round_trip() -> None:
    row = AgentPerformance(
        agent_id="trading-agent",
        time=datetime.now(UTC),
        hit_rate_30d=0.7,
        sharpe_30d=1.4,
        pnl_30d=12.5,
        n_samples=10,
    )
    assert AgentPerformance.model_validate_json(row.model_dump_json()) == row


def test_outcome_fetcher_task_round_trip() -> None:
    now = datetime.now(UTC)
    task = OutcomeFetcherTask(
        cycle_id="eval-1",
        cycle_clock=now.isoformat(),
        high_water_mark=None,
        lookback_days=30,
        candidate_predictions=[_resolved()],
    )
    assert OutcomeFetcherTask.model_validate_json(task.model_dump_json()) == task


def test_outcome_fetcher_output_round_trip() -> None:
    now = datetime.now(UTC)
    out = OutcomeFetcherOutput(resolved_predictions=[_resolved()], next_high_water_mark=now)
    assert OutcomeFetcherOutput.model_validate_json(out.model_dump_json()) == out


def test_pnl_aggregator_round_trip() -> None:
    task = PnlAggregatorTask(cycle_id="eval-1", resolved_predictions=[_resolved()])
    out = PnlAggregatorOutput(
        by_agent=[
            PerAgentPnL(
                agent_id="trading-agent",
                realized_pnl_30d=1.5,
                n_resolved_30d=3,
                n_wins_30d=2,
                pnl_per_trade=[0.5, 0.5, 0.5],
            ),
        ],
    )
    assert PnlAggregatorTask.model_validate_json(task.model_dump_json()) == task
    assert PnlAggregatorOutput.model_validate_json(out.model_dump_json()) == out


def test_agent_performance_task_round_trip() -> None:
    task = AgentPerformanceTask(
        cycle_id="eval-1",
        snapshot_time=datetime.now(UTC),
        by_agent=[
            PerAgentPnL(
                agent_id="trading-agent",
                realized_pnl_30d=0.0,
                n_resolved_30d=0,
                n_wins_30d=0,
                pnl_per_trade=[],
            ),
        ],
    )
    assert AgentPerformanceTask.model_validate_json(task.model_dump_json()) == task


def test_agent_performance_output_round_trip() -> None:
    out = AgentPerformanceOutput(
        rows=[
            AgentPerformanceRow(
                agent_id="trading-agent",
                hit_rate_30d=None,
                sharpe_30d=None,
                pnl_30d=0.0,
                n_samples=0,
            ),
        ],
    )
    assert AgentPerformanceOutput.model_validate_json(out.model_dump_json()) == out


def test_p_raw_must_be_in_unit_interval() -> None:
    with pytest.raises(ValidationError):
        ResolvedPrediction(
            prediction_id=uuid4(),
            market_id="m1",
            agent_id="trading-agent",
            p_raw=1.5,
            outcome=True,
            resolved_at=datetime.now(UTC),
        )


def test_hit_rate_bounded() -> None:
    with pytest.raises(ValidationError):
        AgentPerformanceRow(agent_id="trading-agent", hit_rate_30d=1.5, pnl_30d=0.0)
