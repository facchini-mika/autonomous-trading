"""Cycle-cap gate — caps total notional opened in one trading cycle.

The Lead resets `state.cycle_notional_opened = 0` at the start of each
cycle and increments it after every successful order placement.
"""

from __future__ import annotations

from risk.limits import CYCLE_CAP
from shared.models import GateResult, Order, PortfolioState


def evaluate(state: PortfolioState, order: Order) -> GateResult:
    cap = CYCLE_CAP * state.equity
    existing = state.cycle_notional_opened
    proposed = order.notional_usd

    if existing >= cap:
        return GateResult(
            gate_name="cycle_cap",
            passed=False,
            reason=(f"cycle notional {existing:.2f} already at or above cap {cap:.2f}"),
        )

    headroom = cap - existing
    if proposed <= headroom:
        return GateResult(
            gate_name="cycle_cap",
            passed=True,
            reason=f"within cycle headroom {headroom:.2f}",
        )

    return GateResult(
        gate_name="cycle_cap",
        passed=True,
        reason=f"clipped to remaining cycle headroom {headroom:.2f}",
        clipped_notional=headroom,
    )
