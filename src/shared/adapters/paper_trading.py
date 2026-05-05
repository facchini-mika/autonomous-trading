"""PaperTradingAdapter — composes a live adapter for reads, redirects writes.

Reads are forwarded explicitly to the injected `live_adapter` so the simulated
universe matches reality. Writes (`place_order`, `cancel_order`) skip the venue
entirely and persist to the `paper_trades` table with `broker_order_id`
prefixed `paper-` for easy log-grepping.

Paper writes simulate Polymarket's TIF=FAK (Fill-And-Kill / IOC) behavior:
the fill is clipped to the available top-of-book depth at or better than the
order's limit price; the unfilled remainder is *not* persisted (no resting
orders, by design — see `specs/data_infrastructure.md §2`). Because our
`Orderbook` model carries top-of-book + ±1% aggregate depth (not multi-level),
fill_price is simplified to `best_ask` (BUY) / `best_bid` (SELL) rather than
a true volume-weighted average — conservative and matches the spec for MVP.

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
        fee_rate_bps: int = 0,
    ) -> None:
        self._live = live_adapter
        self._session_factory = session_factory
        self._decision_id_provider = decision_id_provider
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fee_rate_bps = fee_rate_bps

    # --- Reads (forward) -----------------------------------------------------

    def get_markets(self, *, limit: int) -> list[Market]:
        return self._live.get_markets(limit=limit)

    def get_orderbook(self, market_id: str) -> Orderbook:
        return self._live.get_orderbook(market_id)

    def get_metadata(self, market_id: str) -> MarketMetadata:
        return self._live.get_metadata(market_id)

    def get_resolution(self, market_id: str) -> Resolution | None:
        return self._live.get_resolution(market_id)

    def estimate_fee(self, order: Order, *, filled_notional: float | None = None) -> float:
        """Return the simulated paper fee.

        Paper-mode fees are deterministic — we don't call the live adapter
        here so paper cycles stay reproducible. ``fee_rate_bps`` is wired
        from ``Settings.FEE_RATE_BPS`` by the factory. ``filled_notional``
        overrides ``order.notional_usd`` so fees track the actual fill on
        partial-fill outcomes (FAK: unfilled remainder is cancelled, no fee).
        """
        notional = float(order.notional_usd) if filled_notional is None else filled_notional
        return notional * self._fee_rate_bps / 10000.0

    # --- Writes (redirect) ---------------------------------------------------

    def place_order(self, order: Order) -> OrderResult:
        book = self._live.get_orderbook(order.market_id)
        filled_size, fill_price = _simulate_fak_fill(book, order)

        if filled_size <= 0.0 or fill_price is None:
            return OrderResult(
                status="rejected",
                fill_price=None,
                filled_size=0.0,
                fees=0.0,
                broker_order_id=None,
            )

        decision_id = self._decision_id_provider()
        broker_id = f"{PAPER_BROKER_PREFIX}{uuid.uuid4()}"
        now = self._clock()
        filled_notional = filled_size * fill_price
        fees = self.estimate_fee(order, filled_notional=filled_notional)
        status = "filled" if filled_size >= order.size else "partial"

        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO paper_trades (
                        decision_id, market_id, side, size, price, notional_usd,
                        fees, gas, status, broker_order_id, created_at, filled_at
                    ) VALUES (
                        :decision_id, :market_id, :side, :size, :price, :notional_usd,
                        :fees, 0, :status, :broker_order_id, :now, :now
                    )
                    """,
                ),
                {
                    "decision_id": str(decision_id),
                    "market_id": order.market_id,
                    "side": order.side,
                    "size": filled_size,
                    "price": fill_price,
                    "notional_usd": filled_notional,
                    "fees": fees,
                    "status": status,
                    "broker_order_id": broker_id,
                    "now": now,
                },
            )

        return OrderResult(
            status=status,
            fill_price=fill_price,
            filled_size=filled_size,
            fees=fees,
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


def _simulate_fak_fill(book: Orderbook, order: Order) -> tuple[float, float | None]:
    """Clip an order against top-of-book depth — FAK semantics.

    Returns ``(filled_size, fill_price)``. ``order.side="yes"`` means BUY
    (consumes the ask side); ``order.side="no"`` means SELL (consumes the
    bid side). If the limit price isn't marketable against the top, the
    order is rejected (filled_size=0, fill_price=None). Otherwise the
    available ±1% depth caps the fill; the remainder is cancelled by FAK.
    """
    if order.side == "yes":
        if order.price < book.best_ask:
            return 0.0, None
        available = book.depth_ask_1pct
        fill_price = book.best_ask
    else:
        if order.price > book.best_bid:
            return 0.0, None
        available = book.depth_bid_1pct
        fill_price = book.best_bid

    filled_size = min(order.size, available)
    if filled_size <= 0.0:
        return 0.0, None
    return filled_size, fill_price
