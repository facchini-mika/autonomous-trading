"""In-memory FakeAdapter used by tests + Phase 4 Stream D as a stand-in.

Conformance to `PredictionMarketAdapter` is asserted in
`tests/adapters/test_fake_adapter.py` (statically via mypy and dynamically
via `runtime_checkable`).
"""

from __future__ import annotations

import uuid

from shared.models import (
    CancelResult,
    Market,
    MarketMetadata,
    Order,
    Orderbook,
    OrderResult,
    Resolution,
)


class FakeAdapter:
    """Test double for `PredictionMarketAdapter`."""

    def __init__(self, *, fee_rate_bps: int = 0) -> None:
        self.markets: dict[str, Market] = {}
        self.orderbooks: dict[str, Orderbook] = {}
        self.metadata: dict[str, MarketMetadata] = {}
        self.resolutions: dict[str, Resolution] = {}
        self.placed_orders: list[Order] = []
        self.cancelled_orders: list[str] = []
        self.fee_rate_bps = fee_rate_bps
        self.fee_estimate_calls: list[Order] = []

    def get_markets(self, *, limit: int) -> list[Market]:
        return list(self.markets.values())[:limit]

    def get_orderbook(self, market_id: str) -> Orderbook:
        return self.orderbooks[market_id]

    def get_metadata(self, market_id: str) -> MarketMetadata:
        return self.metadata[market_id]

    def get_resolution(self, market_id: str) -> Resolution | None:
        return self.resolutions.get(market_id)

    def estimate_fee(self, order: Order) -> float:
        self.fee_estimate_calls.append(order)
        return order.notional_usd * self.fee_rate_bps / 10000.0

    def place_order(self, order: Order) -> OrderResult:
        self.placed_orders.append(order)
        return OrderResult(
            status="filled",
            fill_price=order.price,
            filled_size=order.size,
            fees=0.0,
            broker_order_id=str(uuid.uuid4()),
        )

    def cancel_order(self, order_id: str) -> CancelResult:
        self.cancelled_orders.append(order_id)
        return CancelResult(
            order_id=order_id,
            status="cancelled",
            cancelled_size=0.0,
        )
