"""Kill switch — global block flipped via `system_state(key='kill_switch')`.

The Lead reads `kill_switch` from Postgres once per cycle and injects the
boolean into `PortfolioState.kill_switch_active`. `risk/` itself never
touches the database — the import-linter risk-isolation contract enforces
this.
"""

from __future__ import annotations

from shared.models import GateResult, Order, PortfolioState


def evaluate(state: PortfolioState, order: Order) -> GateResult:  # noqa: ARG001
    if state.kill_switch_active:
        return GateResult(
            gate_name="kill_switch",
            passed=False,
            reason="kill_switch is active — operator must clear system_state",
        )

    return GateResult(
        gate_name="kill_switch",
        passed=True,
        reason="kill_switch inactive",
    )
