"""Agent input/output shapes that flow through the trading cycle."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.models.market import Market, Orderbook
from shared.models.order import OrderSide, OrderStatus

DecisionAction = Literal["trade", "skip", "hold"]
LessonStatus = Literal["open", "merged", "rejected", "superseded", "incorporated"]


class SizingProposal(BaseModel):
    """Lead-computed sizing proposal handed to risk-execution per prediction.

    The trading-agent does not propose sizing; the Lead derives it via
    ``risk.sizing.propose_notional`` between the trading-agent and
    risk-execution invocations. Risk-execution then runs the gates against
    this proposal and may clip it.
    """

    model_config = ConfigDict(frozen=True)

    market_id: str
    prediction_id: UUID
    proposed_notional_usd: float = Field(gt=0.0)
    side: OrderSide
    q_market: float = Field(gt=0.0, lt=1.0)
    fee_estimate_usd: float = Field(default=0.0, ge=0.0)


class Universe(BaseModel):
    """Top-K liquid markets snapshot handed from the Lead's deterministic scanner to the trading-agent."""

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
    cycle_id: str | None = None
    outcome: bool | None = None
    realized_pnl: float | None = None
    created_at: datetime


class Decision(BaseModel):
    """Cycle-level aggregated decision per market with gate evaluations."""

    model_config = ConfigDict(frozen=True)

    # Risk-execution-LLM was observed emitting non-canonical sizing keys
    # (``final_notional_usd`` / ``clipped_notional_usd`` in cycle-7,
    # ``notional_clipped_usd`` in cycle-1779210369, nested under a ``sizing``
    # sub-dict in cycle-1779223256) instead of the canonical ``clipped_notional``
    # / ``notional_usd`` the Lead's ``_decision_notional`` reads. Doctrine alone
    # (PR #46) did not stop the drift. This map lists known aliases per
    # canonical key, in lookup priority order; aliases are searched both at
    # ``gate_results`` top level and inside the nested ``sizing`` sub-dict.
    _NOTIONAL_ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {
        "clipped_notional": (
            "clipped_notional_usd",
            "notional_clipped_usd",
            "final_notional_usd",
        ),
        "notional_usd": (
            "final_notional_usd",
            "clipped_notional_usd",
            "notional_clipped_usd",
            "proposed_notional_usd",
        ),
    }

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

    @field_validator("gate_results", mode="before")
    @classmethod
    def _normalize_notional_aliases(cls, v: Any) -> Any:
        """Hoist non-canonical notional keys into canonical names at parse-time.

        Canonical wins over alias if both are present and positive. Zero or
        non-numeric values are not hoisted (the Lead reads positive-only).
        Aliases are searched first at ``gate_results`` top level, then inside
        the nested ``sizing`` sub-dict; top-level matches take precedence so
        a misplaced top-level value still wins over a stale nested copy.
        Non-dict input passes through untouched (Pydantic will raise on
        type mismatch downstream).
        """
        if not isinstance(v, dict):
            return v
        sources = [v]
        sizing = v.get("sizing")
        if isinstance(sizing, dict):
            sources.append(sizing)
        for canonical, aliases in cls._NOTIONAL_ALIASES.items():
            if cls._has_positive_number(v.get(canonical)):
                continue
            hoisted = cls._first_positive(sources, aliases)
            if hoisted is not None:
                v[canonical] = hoisted
        return v

    @classmethod
    def _first_positive(cls, sources: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    return float(value)
        return None

    @staticmethod
    def _has_positive_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


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
