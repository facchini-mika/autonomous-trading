"""Inter-agent contracts for the Tier-1 Trade Evaluation Team.

Mirrors ``shared.models.tasks`` for the trading pipeline: each subagent has a
typed Task (Lead → subagent payload) and a typed Output (subagent → Lead
artifact). The Lead pre-fetches all source rows from the DB and injects them
into the task payload, so subagents never touch the database directly
(matches the ``tools: []`` Constraint in their ``.claude/agents/*.md``
definitions).

Subagent topology, per ``trading_feedback.md §6``:

  outcome-fetcher           pnl-aggregator              agent-performance-updater
  -----------------------   -------------------------   --------------------------
  read predictions where    compute per-agent realized  fold per-agent series into
  outcome IS NOT NULL,      PnL series + win counts     (hit_rate, sharpe, pnl,
  filter to lookback window for the lookback window     n_samples) rows
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ResolvedPrediction(BaseModel):
    """One resolved prediction the outcome-fetcher hands forward."""

    model_config = ConfigDict(frozen=True)

    prediction_id: UUID
    market_id: str
    agent_id: str
    p_raw: float = Field(ge=0.0, le=1.0)
    outcome: bool
    realized_pnl: float | None = None
    resolved_at: datetime


class OutcomeFetcherTask(BaseModel):
    """Lead → outcome-fetcher payload."""

    model_config = ConfigDict(frozen=True)

    cycle_id: str
    cycle_clock: str
    high_water_mark: datetime | None
    lookback_days: int = Field(ge=1)
    candidate_predictions: list[ResolvedPrediction]


class OutcomeFetcherOutput(BaseModel):
    """outcome-fetcher → Lead. Curated subset of candidates plus next HWM."""

    model_config = ConfigDict(frozen=True)

    resolved_predictions: list[ResolvedPrediction]
    next_high_water_mark: datetime


class PerAgentPnL(BaseModel):
    """Per-agent rolling-window aggregate from the pnl-aggregator."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    realized_pnl_30d: float
    n_resolved_30d: int = Field(ge=0)
    n_wins_30d: int = Field(ge=0)
    pnl_per_trade: list[float]


class PnlAggregatorTask(BaseModel):
    """Lead → pnl-aggregator payload."""

    model_config = ConfigDict(frozen=True)

    cycle_id: str
    resolved_predictions: list[ResolvedPrediction]


class PnlAggregatorOutput(BaseModel):
    """pnl-aggregator → Lead."""

    model_config = ConfigDict(frozen=True)

    by_agent: list[PerAgentPnL]


class AgentPerformanceRow(BaseModel):
    """One row the agent-performance-updater wants persisted."""

    model_config = ConfigDict(frozen=True)

    agent_id: str
    hit_rate_30d: float | None = Field(default=None, ge=0.0, le=1.0)
    sharpe_30d: float | None = None
    pnl_30d: float
    n_samples: int = Field(default=0, ge=0)


class AgentPerformanceTask(BaseModel):
    """Lead → agent-performance-updater payload."""

    model_config = ConfigDict(frozen=True)

    cycle_id: str
    snapshot_time: datetime
    by_agent: list[PerAgentPnL]


class AgentPerformanceOutput(BaseModel):
    """agent-performance-updater → Lead."""

    model_config = ConfigDict(frozen=True)

    rows: list[AgentPerformanceRow]
