"""Risk-gate evaluation result."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

GateName = Literal[
    "kill_switch",
    "capital",
    "concentration",
    "solvency",
    "cycle_cap",
    "sanity",
]


class GateResult(BaseModel):
    """Outcome of one risk-gate evaluation."""

    model_config = ConfigDict(frozen=True)

    gate_name: GateName
    passed: bool
    reason: str
    clipped_notional: float | None = None
    requires_approval: bool = False
