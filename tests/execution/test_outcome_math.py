"""Property-based tests for PnL math."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from execution.outcome_math import mark_to_market_bid, realized_pnl

PRICE = st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False)
SIZE = st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False, allow_infinity=False)
FEE = st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)


@given(size=SIZE, entry=PRICE, fees=FEE)
def test_yes_wins_yes_position_pnl(size: float, entry: float, fees: float) -> None:
    pnl = realized_pnl(side="yes", size=size, entry_price=entry, outcome_yes=True, fees=fees)
    assert pnl == pytest.approx(size * (1.0 - entry) - fees, rel=1e-9, abs=1e-9)


@given(size=SIZE, entry=PRICE, fees=FEE)
def test_yes_loses_yes_position_pnl(size: float, entry: float, fees: float) -> None:
    pnl = realized_pnl(side="yes", size=size, entry_price=entry, outcome_yes=False, fees=fees)
    assert pnl == pytest.approx(size * (-entry) - fees, rel=1e-9, abs=1e-9)


@given(size=SIZE, entry=PRICE, fees=FEE)
def test_no_wins_no_position_pnl(size: float, entry: float, fees: float) -> None:
    pnl = realized_pnl(side="no", size=size, entry_price=entry, outcome_yes=False, fees=fees)
    assert pnl == pytest.approx(size * (1.0 - entry) - fees, rel=1e-9, abs=1e-9)


@given(size=SIZE, entry=PRICE, fees=FEE)
def test_no_loses_no_position_pnl(size: float, entry: float, fees: float) -> None:
    pnl = realized_pnl(side="no", size=size, entry_price=entry, outcome_yes=True, fees=fees)
    assert pnl == pytest.approx(size * (-entry) - fees, rel=1e-9, abs=1e-9)


@given(size=SIZE, entry=PRICE)
def test_fees_are_monotone(size: float, entry: float) -> None:
    p0 = realized_pnl(side="yes", size=size, entry_price=entry, outcome_yes=True, fees=0)
    p1 = realized_pnl(side="yes", size=size, entry_price=entry, outcome_yes=True, fees=1)
    assert p1 == pytest.approx(p0 - 1, rel=1e-9, abs=1e-9)


def test_invalid_side_rejected() -> None:
    with pytest.raises(ValueError, match="side"):
        realized_pnl(side="maybe", size=1, entry_price=0.5, outcome_yes=True)


def test_invalid_size_rejected() -> None:
    with pytest.raises(ValueError, match="size"):
        realized_pnl(side="yes", size=-1, entry_price=0.5, outcome_yes=True)


def test_invalid_price_rejected() -> None:
    with pytest.raises(ValueError, match="entry_price"):
        realized_pnl(side="yes", size=1, entry_price=1.5, outcome_yes=True)


@given(size=SIZE, entry=PRICE, bid=PRICE)
def test_mtm_yes_uses_bid_directly(size: float, entry: float, bid: float) -> None:
    mtm = mark_to_market_bid(side="yes", size=size, entry_price=entry, current_bid=bid)
    assert mtm == pytest.approx(size * (bid - entry), rel=1e-9, abs=1e-9)


@given(size=SIZE, entry=PRICE, bid=PRICE)
def test_mtm_no_uses_one_minus_bid(size: float, entry: float, bid: float) -> None:
    mtm = mark_to_market_bid(side="no", size=size, entry_price=entry, current_bid=bid)
    assert mtm == pytest.approx(size * ((1.0 - bid) - entry), rel=1e-9, abs=1e-9)


def test_mtm_invalid_bid_rejected() -> None:
    with pytest.raises(ValueError, match="current_bid"):
        mark_to_market_bid(side="yes", size=1, entry_price=0.5, current_bid=1.5)


def test_pnl_is_finite() -> None:
    pnl = realized_pnl(side="yes", size=10, entry_price=0.5, outcome_yes=True, fees=0.1, gas=0.05)
    assert math.isfinite(pnl)
