"""Shared hypothesis strategies for risk-gate property tests."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import strategies as st

from shared.config.settings import TradingMode
from shared.models import CashBalance, Order, PortfolioState, Position
from shared.models.order import OrderSide
from shared.models.portfolio import PositionSide, PositionStatus

# Reasonable financial bounds. NaN/Inf disallowed everywhere — gates assume
# well-formed numbers.
NOTIONAL = st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False)
EQUITY = st.floats(min_value=1.0, max_value=1e7, allow_nan=False, allow_infinity=False)
PRICE = st.floats(min_value=0.001, max_value=0.999, allow_nan=False, allow_infinity=False)
SIZE = st.floats(min_value=0.01, max_value=1e5, allow_nan=False, allow_infinity=False)


def make_cash(available: float = 1_000_000.0) -> CashBalance:
    return CashBalance(
        total_usd=available,
        available=available,
        reserved_for_orders=0.0,
        timestamp=datetime.now(UTC),
    )


def make_position(
    market_id: str = "m1",
    side: PositionSide = "yes",
    size: float = 10.0,
    avg_price: float = 0.5,
    *,
    status: PositionStatus = "open",
) -> Position:
    now = datetime.now(UTC)
    return Position(
        market_id=market_id,
        side=side,
        size=size,
        avg_price=avg_price,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        opened_at=now,
        last_updated=now,
        status=status,
    )


def make_state(
    *,
    equity: float = 10_000.0,
    positions: list[Position] | None = None,
    cash_available: float | None = None,
    cycle_notional_opened: float = 0.0,
    kill_switch_active: bool = False,
    trading_mode: TradingMode = "paper",
    orders_in_last_hour: int = 0,
    gross_exposure_usd: float = 0.0,
) -> PortfolioState:
    cash_avail = cash_available if cash_available is not None else equity
    return PortfolioState(
        cash=make_cash(cash_avail),
        positions=positions or [],
        gross_exposure_usd=gross_exposure_usd,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        equity=equity,
        cycle_notional_opened=cycle_notional_opened,
        kill_switch_active=kill_switch_active,
        trading_mode=trading_mode,
        orders_in_last_hour=orders_in_last_hour,
        timestamp=datetime.now(UTC),
    )


def make_order(
    *,
    market_id: str = "m1",
    side: OrderSide = "yes",
    size: float = 10.0,
    price: float = 0.5,
    notional_usd: float | None = None,
    idempotency_key: str = "test",
) -> Order:
    notional = notional_usd if notional_usd is not None else size * price
    return Order(
        market_id=market_id,
        side=side,
        size=size,
        price=price,
        notional_usd=notional,
        idempotency_key=idempotency_key,
    )
