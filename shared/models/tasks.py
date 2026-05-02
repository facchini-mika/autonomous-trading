"""Task wrapper models for agent-team hook validation.

The Phase-3d hooks `task_created_validate` and `task_completed_validate`
route stdin payloads by `subagent_type` and validate against the matching
model below.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from shared.models.agent_io import Decision, Prediction, Trade, Universe
from shared.models.portfolio import PortfolioState


class ScannerReviewerTask(BaseModel):
    """Lead → scanner-reviewer task payload."""

    model_config = ConfigDict(frozen=True)

    top_k: int
    cycle_clock: str


class TradingAgentTask(BaseModel):
    """Lead → trading-agent task payload."""

    model_config = ConfigDict(frozen=True)

    universe: Universe
    portfolio_state: PortfolioState


class RiskExecutionTask(BaseModel):
    """Lead → risk-execution task payload."""

    model_config = ConfigDict(frozen=True)

    predictions: list[Prediction]
    portfolio_state: PortfolioState


class ScannerReviewerOutput(BaseModel):
    """scanner-reviewer → Lead output."""

    model_config = ConfigDict(frozen=True)

    universe: Universe
    portfolio_state: PortfolioState


class TradingAgentOutput(BaseModel):
    """trading-agent → Lead output."""

    model_config = ConfigDict(frozen=True)

    predictions: list[Prediction]


class RiskExecutionOutput(BaseModel):
    """risk-execution → Lead output."""

    model_config = ConfigDict(frozen=True)

    decisions: list[Decision]
    trades: list[Trade]
