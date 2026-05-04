"""Properties for risk.solvency_gate."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from risk.solvency_gate import evaluate
from tests.risk.conftest import NOTIONAL, make_order, make_state


@given(
    available=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    notional=NOTIONAL,
)
def test_pass_iff_cash_covers_notional(available: float, notional: float) -> None:
    state = make_state(equity=available + 1.0, cash_available=available)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed == (available >= notional)


def test_zero_cash_blocks_any_order() -> None:
    state = make_state(equity=100.0, cash_available=0.0)
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is False
    assert "available cash" in result.reason


def test_cash_equal_to_notional_passes() -> None:
    state = make_state(equity=100.0, cash_available=50.0)
    order = make_order(notional_usd=50.0)
    result = evaluate(state, order)
    assert result.passed is True


def test_fee_estimate_pushes_required_above_cash() -> None:
    state = make_state(equity=100.0, cash_available=50.0)
    order = make_order(notional_usd=49.0)
    # Without fee, 49 <= 50 — passes.
    assert evaluate(state, order, fee_estimate=0.0).passed is True
    # With a 2 USD fee, 49 + 2 = 51 > 50 — fails.
    result = evaluate(state, order, fee_estimate=2.0)
    assert result.passed is False
    assert "notional+fees" in result.reason


def test_fee_estimate_default_is_zero_for_backwards_compat() -> None:
    state = make_state(equity=100.0, cash_available=50.0)
    order = make_order(notional_usd=49.0)
    # No fee_estimate kwarg → default 0 → existing behaviour preserved.
    assert evaluate(state, order).passed is True


def test_negative_fee_estimate_rejected() -> None:
    state = make_state(equity=100.0, cash_available=1_000.0)
    order = make_order(notional_usd=1.0)
    result = evaluate(state, order, fee_estimate=-0.01)
    assert result.passed is False
    assert "negative" in result.reason


@given(
    available=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    notional=NOTIONAL,
    fee=st.floats(min_value=0.0, max_value=1e4, allow_nan=False, allow_infinity=False),
)
def test_pass_iff_cash_covers_notional_plus_fee(available: float, notional: float, fee: float) -> None:
    state = make_state(equity=available + notional + fee + 1.0, cash_available=available)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order, fee_estimate=fee)
    assert result.passed == (available >= notional + fee)
