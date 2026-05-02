"""Properties for risk.capital_gate."""

from __future__ import annotations

from hypothesis import given

from risk.capital_gate import MAX_CAPITAL_EUR, evaluate
from tests.risk.conftest import EQUITY, NOTIONAL, make_order, make_state


def test_constant_is_zero_by_default() -> None:
    assert MAX_CAPITAL_EUR == 0.0


@given(equity=EQUITY, notional=NOTIONAL)
def test_paper_mode_always_passes(equity: float, notional: float) -> None:
    state = make_state(equity=equity, trading_mode="paper")
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is True
    assert result.gate_name == "capital"
    assert result.reason == "paper_mode_bypass"


@given(notional=NOTIONAL)
def test_real_capital_with_max_zero_blocks_everything(notional: float) -> None:
    state = make_state(trading_mode="real_capital", gross_exposure_usd=0.0)
    order = make_order(notional_usd=notional)
    result = evaluate(state, order)
    assert result.passed is False
    assert result.gate_name == "capital"
    assert "MAX_CAPITAL_EUR=0.0" in result.reason


def test_real_capital_within_max_passes() -> None:
    # Hypothetical scenario where Operator has raised MAX > 0. Use module
    # patching to verify the under-cap branch independently of the default.
    import risk.capital_gate as gate

    original = gate.MAX_CAPITAL_EUR
    try:
        gate.MAX_CAPITAL_EUR = 1000.0  # type: ignore[misc]
        state = make_state(trading_mode="real_capital", gross_exposure_usd=100.0)
        order = make_order(notional_usd=50.0)
        result = gate.evaluate(state, order)
        assert result.passed is True
        assert "within MAX_CAPITAL_EUR" in result.reason
    finally:
        gate.MAX_CAPITAL_EUR = original  # type: ignore[misc]


def test_real_capital_at_max_passes() -> None:
    import risk.capital_gate as gate

    original = gate.MAX_CAPITAL_EUR
    try:
        gate.MAX_CAPITAL_EUR = 100.0  # type: ignore[misc]
        state = make_state(trading_mode="real_capital", gross_exposure_usd=70.0)
        order = make_order(notional_usd=30.0)
        result = gate.evaluate(state, order)
        assert result.passed is True
    finally:
        gate.MAX_CAPITAL_EUR = original  # type: ignore[misc]
