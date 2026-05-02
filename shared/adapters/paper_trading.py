"""PaperTradingAdapter — composes a live adapter for reads, redirects writes.

Reads are forwarded explicitly to the injected `live_adapter` so the simulated
universe matches reality. Writes (`place_order`, `cancel_order`) skip the venue
entirely and persist to the `paper_trades` table with `broker_order_id`
prefixed `paper-` for easy log-grepping.

`decision_id` is supplied via an injected `Callable[[], UUID]` (Stream D
provides the ContextVar-backed implementation). The adapter never imports
`shared.db`; the caller injects a `session_factory` callable that yields a
SQLAlchemy session pre-bound to the `trading_cycle` role.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from sqlalchemy import text

from shared.models import (
    CancelResult,
    Market,
    MarketMetadata,
    Order,
    Orderbook,
    OrderResult,
    Resolution,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from uuid import UUID

    from sqlalchemy.orm import Session

    from shared.adapters.prediction_market import PredictionMarketAdapter

PAPER_BROKER_PREFIX: Final = "paper-"


class PaperTradingAdapter:
    """`PredictionMarketAdapter` that reads from a live adapter, writes to DB."""

    def __init__(
        self,
        *,
        live_adapter: PredictionMarketAdapter,
        session_factory: Callable[[], AbstractContextManager[Session]],
        decision_id_provider: Callable[[], UUID],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._live = live_adapter
        self._session_factory = session_factory
        self._decision_id_provider = decision_id_provider
        self._clock = clock or (lambda: datetime.now(UTC))

    # --- Reads (forward) -----------------------------------------------------

    def get_markets(self, *, limit: int) -> list[Market]:
        return self._live.get_markets(limit=limit)

    def get_orderbook(self, market_id: str) -> Orderbook:
        return self._live.get_orderbook(market_id)

    def get_metadata(self, market_id: str) -> MarketMetadata:
        return self._live.get_metadata(market_id)

    def get_resolution(self, market_id: str) -> Resolution | None:
        return self._live.get_resolution(market_id)

    # --- Writes (redirect) ---------------------------------------------------

    def place_order(self, order: Order) -> OrderResult:
        decision_id = self._decision_id_provider()
        broker_id = f"{PAPER_BROKER_PREFIX}{uuid.uuid4()}"
        now = self._clock()

        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO paper_trades (
                        decision_id, market_id, side, size, price, notional_usd,
                        fees, gas, status, broker_order_id, created_at, filled_at
                    ) VALUES (
                        :decision_id, :market_id, :side, :size, :price, :notional_usd,
                        0, 0, 'filled', :broker_order_id, :now, :now
                    )
                    """,
                ),
                {
                    "decision_id": str(decision_id),
                    "market_id": order.market_id,
                    "side": order.side,
                    "size": order.size,
                    "price": order.price,
                    "notional_usd": order.notional_usd,
                    "broker_order_id": broker_id,
                    "now": now,
                },
            )

        return OrderResult(
            status="filled",
            fill_price=order.price,
            filled_size=order.size,
            fees=0.0,
            broker_order_id=broker_id,
        )

    def cancel_order(self, order_id: str) -> CancelResult:
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    UPDATE paper_trades
                    SET status = 'cancelled'
                    WHERE broker_order_id = :order_id
                    RETURNING size
                    """,
                ),
                {"order_id": order_id},
            )
            row = result.first()
        if row is None:
            return CancelResult(order_id=order_id, status="not_found", cancelled_size=0.0)
        return CancelResult(order_id=order_id, status="cancelled", cancelled_size=float(row[0]))
