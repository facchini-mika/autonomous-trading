"""Edge-proportional sizing — deterministic, no LLM.

The Lead calls `propose_notional` once per `Prediction` between the
trading-agent and risk-execution invocations and packs the result into
the `RiskExecutionTask.proposals` list.

Formula:
    edge = p_yes - q_market
    if |edge| < EDGE_THRESHOLD: return 0.0
    edge_ratio = |edge| / EDGE_THRESHOLD
    base = BASE_TRADE_FRACTION * equity
    raw = base * edge_ratio * EDGE_SIZING_SCALE
    return min(raw, MAX_TRADE_FRACTION * equity)

Stronger conviction (larger |edge|) maps to larger size, capped at
`MAX_TRADE_FRACTION * equity` per single trade. The downstream
concentration / cycle-cap gates may further clip.
"""

from __future__ import annotations

from risk.limits import (
    BASE_TRADE_FRACTION,
    EDGE_SIZING_SCALE,
    EDGE_THRESHOLD,
    MAX_TRADE_FRACTION,
)


def propose_notional(
    *,
    p_yes: float,
    q_market: float,
    equity: float,
) -> float:
    """Return the proposed notional in USD for one trade idea.

    Returns 0.0 when the edge is below threshold (caller should skip the
    trade), the equity is non-positive, or the inputs are nonsensical
    (NaN-style values caught by the math itself; we don't paper over
    upstream bugs).
    """
    if equity <= 0.0:
        return 0.0
    edge = p_yes - q_market
    abs_edge = abs(edge)
    if abs_edge < EDGE_THRESHOLD:
        return 0.0
    edge_ratio = abs_edge / EDGE_THRESHOLD
    base = BASE_TRADE_FRACTION * equity
    raw = base * edge_ratio * EDGE_SIZING_SCALE
    cap = MAX_TRADE_FRACTION * equity
    return min(raw, cap)
