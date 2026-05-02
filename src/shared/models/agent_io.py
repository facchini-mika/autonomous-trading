"""Agent input/output shapes that flow through the trading cycle."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from shared.models.market import Market, Orderbook
from shared.models.order import OrderSide, OrderStatus

DecisionAction = Literal["trade", "skip", "hold"]
LessonStatus = Literal["open", "merged", "rejected", "superseded", "incorporated"]


class Universe(BaseModel):
    """Top-K liquid markets snapshot handed from scanner-reviewer to trading-agent."""

    model_config = ConfigDict(frozen=True)

    markets: list[Market]
    orderbooks: dict[str, Orderbook]
    timestamp: datetime


class Prediction(BaseModel):
    """Trading-agent output: probability estimate + reasoning + provenance."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    market_id: str
    agent_id: str
    p_yes: float = Field(gt=0.0, lt=1.0)
    reasoning: str
    edge: float
    inference_log: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = Field(ge=0)
    outcome: bool | None = None
    realized_pnl: float | None = None
    created_at: datetime


class Decision(BaseModel):
    """Cycle-level aggregated decision per market with gate evaluations."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    cycle_id: str
    market_id: str
    p_consensus: float = Field(gt=0.0, lt=1.0)
    q_market: float = Field(gt=0.0, lt=1.0)
    edge: float
    gate_results: dict[str, Any] = Field(default_factory=dict)
    action: DecisionAction
    rationale: str
    created_at: datetime


class Trade(BaseModel):
    """Persisted record of an order attempt (live or paper)."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    decision_id: UUID
    market_id: str
    side: OrderSide
    size: float = Field(gt=0.0)
    price: float = Field(ge=0.0, le=1.0)
    notional_usd: float = Field(gt=0.0)
    fees: float = Field(default=0.0, ge=0.0)
    gas: float = Field(default=0.0, ge=0.0)
    status: OrderStatus
    broker_order_id: str | None = None
    parent_trade_id: UUID | None = None
    realized_pnl: float | None = None
    created_at: datetime
    filled_at: datetime | None = None


class CyclePlan(BaseModel):
    """Forward-looking plan written by the Lead at end of cycle."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    written_at: datetime
    written_by_cycle_id: str
    next_priorities: list[str] = Field(default_factory=list)
    holds_with_rationale: dict[str, str] = Field(default_factory=dict)
    pending_settlements: dict[str, str] = Field(default_factory=dict)
    opportunities_deferred: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    superseded_at: datetime | None = None


class Note(BaseModel):
    """Per-agent scratchpad row, LRU-bounded ≤50 per agent."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    agent_id: str
    body: str
    tags: list[str] = Field(default_factory=list)
    last_accessed: datetime
    created_at: datetime


class Lesson(BaseModel):
    """Append-only learning row written by daily lessons-summary script."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    source_agent_id: str
    trigger_event_id: UUID | None = None
    market_id: str | None = None
    observation: str
    hypothesis: str | None = None
    action_taken: str | None = None
    outcome: str
    status: LessonStatus = "open"
    parent_pattern_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
