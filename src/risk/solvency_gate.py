"""Solvency gate — block when available cash cannot cover notional + fees.

Composes the Lead-supplied ``fee_estimate`` (built from
``adapter.estimate_fee`` per ``SizingProposal``) into the cash check so
the gate fails before submission rather than letting the venue reject
or overdraw the wallet. No clipping by design — solvency is binary.
"""

from __future__ import annotations

from shared.models import GateResult, Order, PortfolioState


def evaluate(state: PortfolioState, order: Order, fee_estimate: float = 0.0) -> GateResult:
    """Return pass/fail given ``cash >= notional + fee_estimate``.

    ``fee_estimate`` defaults to 0 so legacy callers still type-check; the
    Phase-6c risk-execution flow always passes the Lead-pre-computed value.
    """
    if fee_estimate < 0:
        return GateResult(
            gate_name="solvency",
            passed=False,
            reason=f"fee_estimate {fee_estimate:.4f} is negative",
        )
    required = order.notional_usd + fee_estimate
    if state.cash.available < required:
        return GateResult(
            gate_name="solvency",
            passed=False,
            reason=(
                f"available cash {state.cash.available:.2f} below "
                f"notional+fees {required:.2f} "
                f"(notional={order.notional_usd:.2f}, fee_estimate={fee_estimate:.4f})"
            ),
        )

    return GateResult(
        gate_name="solvency",
        passed=True,
        reason=(
            f"cash {state.cash.available:.2f} covers notional+fees {required:.2f} "
            f"(notional={order.notional_usd:.2f}, fee_estimate={fee_estimate:.4f})"
        ),
    )
