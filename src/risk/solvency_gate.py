"""Solvency gate — block when available cash cannot cover the order notional.

Fees are out of scope here; Phase 4 will compose this gate with an
adapter-supplied fee estimate before submission. No clipping by design —
solvency is binary.
"""

from __future__ import annotations

from shared.models import GateResult, Order, PortfolioState


def evaluate(state: PortfolioState, order: Order) -> GateResult:
    if state.cash.available < order.notional_usd:
        return GateResult(
            gate_name="solvency",
            passed=False,
            reason=(f"available cash {state.cash.available:.2f} below order notional {order.notional_usd:.2f}"),
        )

    return GateResult(
        gate_name="solvency",
        passed=True,
        reason=f"cash {state.cash.available:.2f} covers notional {order.notional_usd:.2f}",
    )
