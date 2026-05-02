"""Properties for risk.concentration_gate."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from risk.concentration_gate import evaluate
from risk.limits import CONCENTRATION_CAP
from tests.risk.conftest import (
    EQUITY,
    make_order,
    make_position,
    make_state,
)

UNDER_FACTOR = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
OVER_FACTOR = st.floats(min_value=1.0001, max_value=100.0, allow_nan=False, allow_infinity=False)


@given(equity=EQUITY, factor=UNDER_FACTOR)
def test_no_existing_position_passes_when_under_cap(equity: float, factor: float) -> None:
    cap = CONCENTRATION_CAP * equity
    notional = max(cap * factor, 0.01)
    notional = min(notional, cap)
    state = make_state(equity=equity)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.clipped_notional is None


@given(equity=EQUITY, factor=OVER_FACTOR)
def test_no_existing_position_clips_when_over_cap(equity: float, factor: float) -> None:
    cap = CONCENTRATION_CAP * equity
    notional = cap * factor
    state = make_state(equity=equity)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.clipped_notional is not None
    assert result.clipped_notional <= cap + 1e-9


@given(
    equity=EQUITY,
    over_cap_factor=st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False),
)
def test_existing_at_or_above_cap_blocks(equity: float, over_cap_factor: float) -> None:
    cap = CONCENTRATION_CAP * equity
    existing_notional = cap * over_cap_factor  # always ≥ cap
    state = make_state(
        equity=equity,
        positions=[make_position(size=existing_notional, avg_price=1.0)],
    )
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is False
    assert result.clipped_notional is None


def test_post_gate_total_never_exceeds_cap() -> None:
    """Manual check: existing + accepted notional ≤ cap (post-clipping)."""
    state = make_state(
        equity=10_000.0,
        positions=[make_position(size=500.0, avg_price=0.5)],  # 250 existing
    )
    cap = CONCENTRATION_CAP * 10_000.0  # 1500
    order = make_order(notional_usd=2000.0)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.clipped_notional is not None
    assert 250.0 + result.clipped_notional <= cap + 1e-9


def test_existing_position_with_other_market_does_not_count() -> None:
    state = make_state(
        equity=10_000.0,
        positions=[make_position(market_id="other-market", size=10000.0, avg_price=1.0)],
    )
    order = make_order(market_id="m1", notional_usd=100.0)
    result = evaluate(state, order)
    assert result.passed is True


def test_existing_position_other_side_does_not_count() -> None:
    state = make_state(
        equity=10_000.0,
        positions=[make_position(side="no", size=10000.0, avg_price=1.0)],
    )
    order = make_order(side="yes", notional_usd=100.0)
    result = evaluate(state, order)
    assert result.passed is True


def test_closed_position_does_not_count() -> None:
    state = make_state(
        equity=10_000.0,
        positions=[make_position(size=10000.0, avg_price=1.0, status="closed")],
    )
    order = make_order(notional_usd=100.0)
    result = evaluate(state, order)
    assert result.passed is True
