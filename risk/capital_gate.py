"""Capital gate — hard cap on real-capital gross exposure.

`MAX_CAPITAL_EUR = 0` is the Phase-3 default and is hardcoded here, not in
`Settings`. Operator must open a 2-reviewer PR with an `AUDIT_LOG.md`
entry to raise it before Phase 6.

Paper-mode bypasses the gate entirely so paper cycles can trade against
`PAPER_STARTING_CASH_USD` without operator intervention.
"""

from __future__ import annotations

from typing import Final

from shared.models import GateResult, Order, PortfolioState

MAX_CAPITAL_EUR: Final[float] = 0.0


def evaluate(state: PortfolioState, order: Order) -> GateResult:
    """Return pass/fail for the capital cap. No clipping."""
    if state.trading_mode == "paper":
        return GateResult(
            gate_name="capital",
            passed=True,
            reason="paper_mode_bypass",
        )

    proposed_total = state.gross_exposure_usd + order.notional_usd
    if proposed_total > MAX_CAPITAL_EUR:
        return GateResult(
            gate_name="capital",
            passed=False,
            reason=(f"real_capital total {proposed_total:.2f} exceeds MAX_CAPITAL_EUR={MAX_CAPITAL_EUR}"),
        )

    return GateResult(
        gate_name="capital",
        passed=True,
        reason=f"within MAX_CAPITAL_EUR={MAX_CAPITAL_EUR}",
    )
