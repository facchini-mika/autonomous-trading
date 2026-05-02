"""Properties for risk.kill_switch."""

from __future__ import annotations

from hypothesis import given

from risk.kill_switch import evaluate
from tests.risk.conftest import NOTIONAL, make_order, make_state


@given(notional=NOTIONAL)
def test_active_blocks_any_order(notional: float) -> None:
    state = make_state(kill_switch_active=True)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is False
    assert result.gate_name == "kill_switch"
    assert "kill_switch" in result.reason.lower()


@given(notional=NOTIONAL)
def test_inactive_passes(notional: float) -> None:
    state = make_state(kill_switch_active=False)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.gate_name == "kill_switch"
