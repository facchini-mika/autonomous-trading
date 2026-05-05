"""Pydantic models — single source of truth for inter-agent contracts."""

from __future__ import annotations

from shared.models.agent_io import (
    CyclePlan,
    Decision,
    DecisionAction,
    Lesson,
    LessonStatus,
    Note,
    Prediction,
    SizingProposal,
    Trade,
    Universe,
)
from shared.models.agent_performance import AgentPerformance
from shared.models.equity_snapshot import EquitySnapshot
from shared.models.eval_io import (
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
from shared.models.gate import GateName, GateResult
from shared.models.market import (
    Market,
    MarketMetadata,
    MarketStatus,
    Orderbook,
    Resolution,
)
from shared.models.order import (
    CancelResult,
    CancelStatus,
    Order,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from shared.models.portfolio import (
    CashBalance,
    PortfolioState,
    Position,
    PositionSide,
    PositionStatus,
)
from shared.models.tasks import (
    RiskExecutionOutput,
    RiskExecutionTask,
    ScannerReviewerOutput,
    ScannerReviewerTask,
    ScannerThresholds,
    TradingAgentOutput,
    TradingAgentTask,
)

__all__ = [
    "AgentPerformance",
    "AgentPerformanceOutput",
    "AgentPerformanceRow",
    "AgentPerformanceTask",
    "CancelResult",
    "CancelStatus",
    "CashBalance",
    "CyclePlan",
    "Decision",
    "DecisionAction",
    "EquitySnapshot",
    "GateName",
    "GateResult",
    "Lesson",
    "LessonStatus",
    "Market",
    "MarketMetadata",
    "MarketStatus",
    "Note",
    "Order",
    "OrderResult",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Orderbook",
    "OutcomeFetcherOutput",
    "OutcomeFetcherTask",
    "PerAgentPnL",
    "PnlAggregatorOutput",
    "PnlAggregatorTask",
    "PortfolioState",
    "Position",
    "PositionSide",
    "PositionStatus",
    "Prediction",
    "Resolution",
    "ResolvedPrediction",
    "RiskExecutionOutput",
    "RiskExecutionTask",
    "ScannerReviewerOutput",
    "ScannerReviewerTask",
    "ScannerThresholds",
    "SizingProposal",
    "Trade",
    "TradingAgentOutput",
    "TradingAgentTask",
    "Universe",
]
