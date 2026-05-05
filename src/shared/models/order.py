"""Order lifecycle shapes: Order, OrderResult, CancelResult."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OrderSide = Literal["yes", "no"]
OrderStatus = Literal["open", "partial", "filled", "cancelled", "rejected"]
# `OrderType` here is the *pricing style* (limit price set to the current best
# ask/bid for immediate execution). Distinct from the Polymarket-CLOB-internal
# `OrderType` (TIF: GTC/FAK/FOK/GTD) — that is hardcoded to FAK in
# `PolymarketAdapter` per `specs/data_infrastructure.md §2`. If a second pricing
# style is ever added, rename this enum to `PricingStyle` and add `Order.tif`
# as a separate field.
OrderType = Literal["marketable_limit"]
CancelStatus = Literal["cancelled", "not_found", "error"]


class Order(BaseModel):
    """Pre-submission order intent."""

    model_config = ConfigDict(frozen=True)

    market_id: str
    side: OrderSide
    size: float = Field(gt=0.0)
    price: float = Field(ge=0.0, le=1.0)
    notional_usd: float = Field(gt=0.0)
    order_type: OrderType = "marketable_limit"
    idempotency_key: str


class OrderResult(BaseModel):
    """Adapter response immediately after submitting an order."""

    model_config = ConfigDict(frozen=True)

    status: OrderStatus
    fill_price: float | None = None
    filled_size: float = Field(default=0.0, ge=0.0)
    fees: float = Field(default=0.0, ge=0.0)
    broker_order_id: str | None = None


class CancelResult(BaseModel):
    """Adapter response after cancel attempt."""

    model_config = ConfigDict(frozen=True)

    order_id: str
    status: CancelStatus
    cancelled_size: float = Field(default=0.0, ge=0.0)
