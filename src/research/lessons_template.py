"""Deterministic lesson-body templates (no LLM).

Stream C must produce byte-identical output for byte-identical input — this
gives the daily cron deterministic re-runs and tests an exact-match
assertion. The templates take frozen Pydantic-style records and emit a single
multi-line string per surprise category.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID


def lesson_body(
    *,
    category: str,
    market_id: str,
    p_yes: float,
    outcome_yes: bool,
    realized_pnl: float | None,
    notional: float,
    edge: float,
    cycle_id: str | None = None,
) -> str:
    """Return a deterministic multi-line lesson body."""
    pnl_str = f"{realized_pnl:+.2f}" if realized_pnl is not None else "n/a"
    outcome_label = "YES" if outcome_yes else "NO"
    cycle_line = f"Cycle: {cycle_id}\n" if cycle_id else ""
    return (
        f"Category: {category}\n"
        f"Market: {market_id}\n"
        f"{cycle_line}"
        f"Predicted p_yes={p_yes:.4f}, edge={edge:+.4f}, notional=${notional:.2f}\n"
        f"Resolved: {outcome_label}\n"
        f"Realized PnL: {pnl_str}\n"
    )


def hypothesis_for(category: str) -> str | None:
    """Pre-canned hypothesis stub keyed off the surprise category."""
    table = {
        "missed_yes": "Signal under-weighted; check whether scanner-reviewer filtered the relevant catalyst.",
        "missed_no": "Signal under-weighted; check whether scanner-reviewer filtered the relevant catalyst.",
        "overconfident_yes": "Inference put too much mass on YES; review the rationale for that prior.",
        "overconfident_no": "Inference put too much mass on NO; review the rationale for that prior.",
        "expected": None,
    }
    return table.get(category)


def action_taken(realized_pnl: float | None) -> str:
    if realized_pnl is None:
        return "no trade was placed"
    if realized_pnl >= 0:
        return f"trade closed with positive PnL ({realized_pnl:+.2f})"
    return f"trade closed with negative PnL ({realized_pnl:+.2f})"


def stable_lesson_id_seed(*, source_agent_id: str, trigger_event_id: UUID | None) -> str:
    """Deterministic seed string used in tests; not stored in the row."""
    return f"{source_agent_id}|{trigger_event_id}"
