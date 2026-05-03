"""Property tests for ``risk.sizing.propose_notional``.

The function is deterministic and central to the cycle's risk profile, so
we exercise it with hypothesis to nail down the invariants:

- non-negative output
- zero output iff edge is sub-threshold (or equity ≤ 0)
- output capped at ``MAX_TRADE_FRACTION × equity``
- monotonic in ``|edge|`` for fixed equity
"""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from risk.limits import (
    BASE_TRADE_FRACTION,
    EDGE_SIZING_SCALE,
    EDGE_THRESHOLD,
    MAX_TRADE_FRACTION,
)
from risk.sizing import propose_notional


def _edge_pair(edge: float) -> tuple[float, float]:
    """Build (p_yes, q_market) such that ``p_yes - q_market == edge``."""
    p_yes = 0.5 + edge / 2
    q_market = 0.5 - edge / 2
    p_yes = min(max(p_yes, 0.001), 0.999)
    q_market = min(max(q_market, 0.001), 0.999)
    return p_yes, q_market


@given(
    p_yes=st.floats(min_value=0.001, max_value=0.999),
    q_market=st.floats(min_value=0.001, max_value=0.999),
    equity=st.floats(min_value=0.0, max_value=1_000_000.0, allow_nan=False),
)
@settings(max_examples=200)
def test_output_is_non_negative_and_capped(p_yes: float, q_market: float, equity: float) -> None:
    notional = propose_notional(p_yes=p_yes, q_market=q_market, equity=equity)
    assert notional >= 0.0
    assert notional <= MAX_TRADE_FRACTION * max(equity, 0.0) + 1e-9


@given(
    edge=st.floats(min_value=-EDGE_THRESHOLD * 0.99, max_value=EDGE_THRESHOLD * 0.99),
    equity=st.floats(min_value=1.0, max_value=1_000_000.0),
)
@settings(max_examples=100)
def test_below_threshold_returns_zero(edge: float, equity: float) -> None:
    p_yes, q_market = _edge_pair(edge)
    notional = propose_notional(p_yes=p_yes, q_market=q_market, equity=equity)
    assert notional == 0.0


def test_zero_equity_returns_zero() -> None:
    p_yes, q_market = _edge_pair(0.10)
    assert propose_notional(p_yes=p_yes, q_market=q_market, equity=0.0) == 0.0


def test_negative_equity_returns_zero() -> None:
    p_yes, q_market = _edge_pair(0.10)
    assert propose_notional(p_yes=p_yes, q_market=q_market, equity=-100.0) == 0.0


def test_at_threshold_uses_base_fraction() -> None:
    equity = 10_000.0
    edge = EDGE_THRESHOLD  # exactly threshold
    p_yes, q_market = _edge_pair(edge)
    notional = propose_notional(p_yes=p_yes, q_market=q_market, equity=equity)
    expected = BASE_TRADE_FRACTION * equity * EDGE_SIZING_SCALE
    assert math.isclose(notional, expected, rel_tol=1e-6)


@given(
    base_edge=st.floats(min_value=EDGE_THRESHOLD * 1.001, max_value=0.45),
    equity=st.floats(min_value=1_000.0, max_value=1_000_000.0),
)
@settings(max_examples=80)
def test_monotonic_in_abs_edge(base_edge: float, equity: float) -> None:
    p_yes_a, q_a = _edge_pair(base_edge)
    p_yes_b, q_b = _edge_pair(base_edge * 1.5 if base_edge < 0.3 else base_edge)
    notional_a = propose_notional(p_yes=p_yes_a, q_market=q_a, equity=equity)
    notional_b = propose_notional(p_yes=p_yes_b, q_market=q_b, equity=equity)
    if base_edge * 1.5 > base_edge:
        assert notional_b >= notional_a - 1e-9


def test_negative_edge_same_size_as_positive() -> None:
    equity = 10_000.0
    pos_p, pos_q = _edge_pair(0.06)
    neg_p, neg_q = _edge_pair(-0.06)
    pos = propose_notional(p_yes=pos_p, q_market=pos_q, equity=equity)
    neg = propose_notional(p_yes=neg_p, q_market=neg_q, equity=equity)
    assert math.isclose(pos, neg, rel_tol=1e-9)


def test_large_edge_capped_at_max_fraction() -> None:
    equity = 10_000.0
    p_yes, q_market = _edge_pair(0.45)
    notional = propose_notional(p_yes=p_yes, q_market=q_market, equity=equity)
    assert notional == MAX_TRADE_FRACTION * equity
