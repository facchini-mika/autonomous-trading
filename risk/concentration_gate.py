"""Concentration gate — caps notional in any single (market, side).

Convention across clipping gates: `passed=True` means execute (possibly at
clipped size); `passed=False` means abort. `clipped_notional=None` means
"use the full proposed notional".
"""

from __future__ import annotations

from risk.limits import CONCENTRATION_CAP
from shared.models import GateResult, Order, PortfolioState


def _existing_notional(state: PortfolioState, order: Order) -> float:
    """Sum of `size * avg_price` for any open position on the same (market, side)."""
    return sum(
        p.size * p.avg_price
        for p in state.positions
        if p.market_id == order.market_id and p.side == order.side and p.status == "open"
    )


def evaluate(state: PortfolioState, order: Order) -> GateResult:
    """Return pass/fail/clip per the concentration cap."""
    cap = CONCENTRATION_CAP * state.equity
    existing = _existing_notional(state, order)
    proposed = order.notional_usd

    if existing >= cap:
        return GateResult(
            gate_name="concentration",
            passed=False,
            reason=(
                f"existing notional {existing:.2f} on ({order.market_id},{order.side}) "
                f"already at or above cap {cap:.2f}"
            ),
        )

    headroom = cap - existing
    if proposed <= headroom:
        return GateResult(
            gate_name="concentration",
            passed=True,
            reason=f"within concentration headroom {headroom:.2f}",
        )

    return GateResult(
        gate_name="concentration",
        passed=True,
        reason=f"clipped to remaining concentration headroom {headroom:.2f}",
        clipped_notional=headroom,
    )
