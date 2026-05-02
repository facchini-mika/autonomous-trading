"""Sanity gates — last-line guards on order shape.

Composes four sub-checks in order: order-size, price, order-rate,
position-count. Returns the first failing result. Position-count is
advisory: it sets `requires_approval=True` but `passed=True` — the Lead
is expected to log/alarm and either escalate to the operator or proceed.
"""

from __future__ import annotations

from risk.limits import (
    MAX_OPEN_POSITIONS,
    MAX_ORDER_PCT_EQUITY,
    ORDER_RATE_LIMIT_PER_HOUR,
    PRICE_MAX,
    PRICE_MIN,
)
from shared.models import GateResult, Order, PortfolioState


def _check_order_size(state: PortfolioState, order: Order) -> GateResult | None:
    cap = MAX_ORDER_PCT_EQUITY * state.equity
    if order.notional_usd > cap:
        return GateResult(
            gate_name="sanity",
            passed=False,
            reason=(
                f"order notional {order.notional_usd:.2f} exceeds "
                f"sanity cap {cap:.2f} ({MAX_ORDER_PCT_EQUITY:.0%} of equity)"
            ),
        )
    return None


def _check_price(order: Order) -> GateResult | None:
    if order.price <= PRICE_MIN or order.price >= PRICE_MAX:
        return GateResult(
            gate_name="sanity",
            passed=False,
            reason=(f"order price {order.price} outside open interval ({PRICE_MIN}, {PRICE_MAX})"),
        )
    return None


def _check_order_rate(state: PortfolioState) -> GateResult | None:
    if state.orders_in_last_hour >= ORDER_RATE_LIMIT_PER_HOUR:
        return GateResult(
            gate_name="sanity",
            passed=False,
            reason=(f"orders_in_last_hour={state.orders_in_last_hour} reached limit {ORDER_RATE_LIMIT_PER_HOUR}"),
        )
    return None


def _check_position_count(state: PortfolioState) -> GateResult | None:
    open_count = sum(1 for p in state.positions if p.status == "open")
    if open_count >= MAX_OPEN_POSITIONS:
        return GateResult(
            gate_name="sanity",
            passed=True,
            reason=(f"open_positions={open_count} at or above {MAX_OPEN_POSITIONS}; operator approval recommended"),
            requires_approval=True,
        )
    return None


def evaluate(state: PortfolioState, order: Order) -> GateResult:
    for result in (
        _check_order_size(state, order),
        _check_price(order),
        _check_order_rate(state),
        _check_position_count(state),
    ):
        if result is not None:
            return result

    return GateResult(
        gate_name="sanity",
        passed=True,
        reason="all sanity sub-checks passed",
    )
