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
    "CancelResult",
    "CancelStatus",
    "CashBalance",
    "CyclePlan",
    "Decision",
    "DecisionAction",
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
    "PortfolioState",
    "Position",
    "PositionSide",
    "PositionStatus",
    "Prediction",
    "Resolution",
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
