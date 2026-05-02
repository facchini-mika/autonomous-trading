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
