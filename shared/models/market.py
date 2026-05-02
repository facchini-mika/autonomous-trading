"""Polymarket market data shapes.

Field-level definitions cross-referenced against trading.md and
data_infrastructure.md §1. Models are frozen for safety; mutations require
`.model_copy(update=...)`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MarketStatus = Literal["open", "resolved", "closed"]


class Market(BaseModel):
    """One Polymarket binary market."""

    model_config = ConfigDict(frozen=True)

    market_id: str
    condition_id: str
    slug: str
    title: str
    category: str
    end_date: datetime
    status: MarketStatus
    resolution_source: str | None = None
    created_at: datetime
    last_seen: datetime
    ambiguity_score: float | None = Field(default=None, ge=0.0, le=1.0)


class Orderbook(BaseModel):
    """One snapshot of the bid/ask top-of-book and ±1% depth."""

    model_config = ConfigDict(frozen=True)

    market_id: str
    best_bid: float = Field(ge=0.0, le=1.0)
    best_ask: float = Field(ge=0.0, le=1.0)
    mid: float = Field(ge=0.0, le=1.0)
    depth_bid_1pct: float = Field(ge=0.0)
    depth_ask_1pct: float = Field(ge=0.0)
    timestamp: datetime


class MarketMetadata(BaseModel):
    """Resolution rules, tags, and dispute history for a market."""

    model_config = ConfigDict(frozen=True)

    market_id: str
    resolution_criteria: str
    category_tags: list[str] = Field(default_factory=list)
    dispute_history: str | None = None
    implied_probability_yes: float = Field(ge=0.0, le=1.0)


class Resolution(BaseModel):
    """Final settlement outcome for a market."""

    model_config = ConfigDict(frozen=True)

    market_id: str
    outcome: bool
    resolved_at: datetime
    settlement_price: float = Field(ge=0.0, le=1.0)
    disputed: bool = False
