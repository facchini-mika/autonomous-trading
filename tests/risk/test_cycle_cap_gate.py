"""Properties for risk.cycle_cap_gate."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from risk.cycle_cap_gate import evaluate
from risk.limits import CYCLE_CAP
from tests.risk.conftest import EQUITY, make_order, make_state

UNDER_FACTOR = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
OVER_FACTOR = st.floats(min_value=1.0001, max_value=100.0, allow_nan=False, allow_infinity=False)
AT_OR_OVER_FACTOR = st.floats(min_value=1.0, max_value=10.0, allow_nan=False, allow_infinity=False)


@given(equity=EQUITY, factor=UNDER_FACTOR)
def test_fresh_cycle_passes_when_under_cap(equity: float, factor: float) -> None:
    cap = CYCLE_CAP * equity
    notional = max(cap * factor, 0.01)
    notional = min(notional, cap)
    state = make_state(equity=equity, cycle_notional_opened=0.0)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.clipped_notional is None


@given(equity=EQUITY, factor=OVER_FACTOR)
def test_fresh_cycle_clips_when_over_cap(equity: float, factor: float) -> None:
    cap = CYCLE_CAP * equity
    notional = cap * factor
    state = make_state(equity=equity, cycle_notional_opened=0.0)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.clipped_notional is not None
    assert result.clipped_notional <= cap + 1e-9


@given(equity=EQUITY, factor=AT_OR_OVER_FACTOR)
def test_blocks_when_already_at_cap(equity: float, factor: float) -> None:
    cap = CYCLE_CAP * equity
    opened = cap * factor
    state = make_state(equity=equity, cycle_notional_opened=opened)
    order = make_order(notional_usd=10.0)
    result = evaluate(state, order)
    assert result.passed is False
    assert result.clipped_notional is None


def test_post_clip_existing_plus_clipped_within_cap() -> None:
    state = make_state(equity=10_000.0, cycle_notional_opened=2000.0)
    cap = CYCLE_CAP * 10_000.0  # 2500
    order = make_order(notional_usd=1000.0)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.clipped_notional is not None
    assert 2000.0 + result.clipped_notional <= cap + 1e-9


def test_under_cap_no_clip() -> None:
    state = make_state(equity=10_000.0, cycle_notional_opened=500.0)
    order = make_order(notional_usd=100.0)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.clipped_notional is None
