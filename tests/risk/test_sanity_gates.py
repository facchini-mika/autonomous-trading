"""Properties for risk.sanity_gates."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from risk.limits import (
    MAX_OPEN_POSITIONS,
    MAX_ORDER_PCT_EQUITY,
    ORDER_RATE_LIMIT_PER_HOUR,
    PRICE_MAX,
    PRICE_MIN,
)
from risk.sanity_gates import evaluate
from tests.risk.conftest import EQUITY, make_order, make_position, make_state


@given(equity=EQUITY)
def test_oversized_order_blocks(equity: float) -> None:
    cap = MAX_ORDER_PCT_EQUITY * equity
    state = make_state(equity=equity)
    # Use a price/size combination that yields an over-cap notional regardless of equity.
    order = make_order(notional_usd=cap * 2.0 + 1.0, price=0.5, size=1.0)
    result = evaluate(state, order)
    assert result.passed is False
    assert "exceeds sanity cap" in result.reason


@given(price=st.sampled_from([PRICE_MIN, PRICE_MIN - 0.001, 0.0, PRICE_MAX, PRICE_MAX + 0.001, 0.999999]))
def test_out_of_range_price_blocks(price: float) -> None:
    state = make_state(equity=10_000.0)
    order = make_order(notional_usd=10.0, price=price, size=1.0)
    result = evaluate(state, order)
    if price <= PRICE_MIN or price >= PRICE_MAX:
        assert result.passed is False
        assert "outside open interval" in result.reason


def test_in_range_price_passes() -> None:
    state = make_state(equity=10_000.0)
    order = make_order(notional_usd=10.0, price=0.5, size=10.0)
    result = evaluate(state, order)
    assert result.passed is True


def test_order_rate_at_limit_blocks() -> None:
    state = make_state(equity=10_000.0, orders_in_last_hour=ORDER_RATE_LIMIT_PER_HOUR)
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is False
    assert "orders_in_last_hour" in result.reason


def test_order_rate_under_limit_passes() -> None:
    state = make_state(equity=10_000.0, orders_in_last_hour=ORDER_RATE_LIMIT_PER_HOUR - 1)
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is True


def test_position_count_at_limit_passes_with_approval() -> None:
    positions = [make_position(market_id=f"m{i}") for i in range(MAX_OPEN_POSITIONS)]
    state = make_state(equity=10_000.0, positions=positions)
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.requires_approval is True
    assert "operator approval" in result.reason


def test_position_count_under_limit_no_approval() -> None:
    positions = [make_position(market_id=f"m{i}") for i in range(MAX_OPEN_POSITIONS - 1)]
    state = make_state(equity=10_000.0, positions=positions)
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.requires_approval is False


def test_closed_positions_do_not_count_toward_limit() -> None:
    positions = [make_position(market_id=f"m{i}", status="closed") for i in range(MAX_OPEN_POSITIONS + 5)]
    state = make_state(equity=10_000.0, positions=positions)
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.requires_approval is False


def test_all_pass_returns_clean_result() -> None:
    state = make_state(equity=10_000.0)
    order = make_order(notional_usd=10.0, price=0.5, size=20.0)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.requires_approval is False
    assert "all sanity sub-checks passed" in result.reason
