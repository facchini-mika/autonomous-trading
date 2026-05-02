"""Portfolio state: cash, positions, derived equity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PositionSide = Literal["yes", "no"]
PositionStatus = Literal["open", "closed"]


class Position(BaseModel):
    """One open or closed market position."""

    model_config = ConfigDict(frozen=True)

    market_id: str
    side: PositionSide
    size: float = Field(ge=0.0)
    avg_price: float = Field(ge=0.0, le=1.0)
    unrealized_pnl: float
    realized_pnl: float
    opened_at: datetime
    last_updated: datetime
    status: PositionStatus = "open"


class CashBalance(BaseModel):
    """Spendable cash decomposed into total, available, and reserved."""

    model_config = ConfigDict(frozen=True)

    total_usd: float
    available: float
    reserved_for_orders: float = Field(ge=0.0)
    timestamp: datetime


class PortfolioState(BaseModel):
    """Aggregated portfolio: cash + positions + derived caps + kill-switch.

    `kill_switch_active` is injected by the Lead from `system_state` before
    risk-gate evaluation so that `risk/` does not need to import `shared.db`
    (enforced by the import-linter risk-isolation contract).
    """

    model_config = ConfigDict(frozen=True)

    cash: CashBalance
    positions: list[Position] = Field(default_factory=list)
    gross_exposure_usd: float = Field(ge=0.0)
    unrealized_pnl: float
    realized_pnl: float
    equity: float
    cycle_notional_opened: float = Field(default=0.0, ge=0.0)
    kill_switch_active: bool = False
    timestamp: datetime
