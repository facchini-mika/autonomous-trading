"""Task wrapper models for agent-team hook validation.

The Phase-3d hooks `task_created_validate` and `task_completed_validate`
route stdin payloads by `subagent_type` and validate against the matching
model below.

Phase 6b (PR 1) extends the task payloads so the Lead can pre-fetch the
full set of inputs each subagent needs:

- `ScannerReviewerTask` carries the raw market/portfolio data the Lead
  pulled from the adapter and DB; the deterministic Python scanner
  (`execution.lead_bootstrap._python_scanner`) filters and synthesises
  the final `Universe` + `PortfolioState` from it. The model name keeps
  the original `ScannerReviewer` prefix for schema-stability after the
  LLM scanner-reviewer subagent was removed.
- `TradingAgentTask` adds cross-cycle memory (`lessons`, `recent_notes`,
  `prev_cycle_plan`) plus the cycle id and edge threshold so the agent
  can run without re-reading settings.
- `RiskExecutionTask` carries the cycle id; sizing proposals are added
  in PR 4.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from shared.models.agent_io import (
    CyclePlan,
    Decision,
    Lesson,
    Note,
    Prediction,
    SizingProposal,
    Universe,
)
from shared.models.market import Market, MarketMetadata, Orderbook
from shared.models.portfolio import CashBalance, PortfolioState, Position


class ScannerThresholds(BaseModel):
    """Filter and ranking thresholds the Lead injects from settings."""

    model_config = ConfigDict(frozen=True)

    min_depth_1pct_usd: float
    max_spread: float
    min_ttr_hours: int
    max_ttr_days: int
    soon_resolve_threshold_days: int
    soon_resolve_boost_multiplier: float


class ScannerReviewerTask(BaseModel):
    """Lead-internal payload for ``_python_scanner``: raw markets, orderbooks,
    metadata, portfolio + cash state, plus the filter/ranking thresholds."""

    model_config = ConfigDict(frozen=True)

    top_k: int
    cycle_id: str
    cycle_clock: str
    raw_markets: list[Market]
    raw_orderbooks: dict[str, Orderbook]
    raw_metadata: dict[str, MarketMetadata]
    current_positions: list[Position]
    current_cash: CashBalance
    kill_switch_active: bool
    held_market_ids: list[str]
    orders_in_last_hour: int
    thresholds: ScannerThresholds


class TradingAgentTask(BaseModel):
    """Lead → trading-agent task payload."""

    model_config = ConfigDict(frozen=True)

    universe: Universe
    portfolio_state: PortfolioState
    lessons: list[Lesson]
    recent_notes: list[Note]
    prev_cycle_plan: CyclePlan | None
    cycle_id: str
    edge_threshold: float


class RiskExecutionTask(BaseModel):
    """Lead → risk-execution task payload."""

    model_config = ConfigDict(frozen=True)

    predictions: list[Prediction]
    portfolio_state: PortfolioState
    cycle_id: str
    proposals: list[SizingProposal]


class ScannerReviewerOutput(BaseModel):
    """``_python_scanner`` output: filtered/ranked ``Universe`` + ``PortfolioState``."""

    model_config = ConfigDict(frozen=True)

    universe: Universe
    portfolio_state: PortfolioState


class TradingAgentOutput(BaseModel):
    """trading-agent → Lead output."""

    model_config = ConfigDict(frozen=True)

    predictions: list[Prediction]


class RiskExecutionOutput(BaseModel):
    """risk-execution → Lead output.

    The agent emits gate-evaluated decisions only. The Lead places trades
    itself from those decisions via the adapter and never reads a trades
    field from this output, so we don't expose one — emitting it would
    invite the agent to hallucinate Trade UUIDs / fill / fee fields it
    cannot know.
    """

    model_config = ConfigDict(frozen=True)

    decisions: list[Decision]
