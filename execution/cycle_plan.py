"""Deterministic cycle-plan synthesis (no LLM).

The Lead calls `synthesize_cycle_plan(...)` at the end of every cycle. The
output is a single `CyclePlan` that the next cycle reads (most recent
WHERE superseded_at IS NULL) to seed its agenda. The function takes only
this cycle's artifacts and the previous plan; it does not query history.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from shared.models import CyclePlan

HIGH_EDGE_THRESHOLD: Final = 0.10

if TYPE_CHECKING:
    from collections.abc import Iterable

    from shared.models import Decision, Position, Prediction, Trade


def synthesize_cycle_plan(
    *,
    cycle_id: str,
    predictions: Iterable[Prediction],
    decisions: Iterable[Decision],
    trades: Iterable[Trade],
    positions: Iterable[Position],
    prev_plan: CyclePlan | None,
    clock: datetime | None = None,
) -> CyclePlan:
    """Build the next CyclePlan from the artifacts of the cycle that just ran."""
    written_at = clock or datetime.now(UTC)
    decisions_list = list(decisions)
    trades_list = list(trades)
    positions_list = list(positions)
    predictions_list = list(predictions)

    holds_with_rationale = {d.market_id: d.rationale for d in decisions_list if d.action == "hold"}
    opportunities_deferred = sorted(
        {d.market_id for d in decisions_list if d.action == "skip"},
    )
    pending_settlements = {
        p.market_id: f"trade {t.id} pending"
        for p in positions_list
        for t in trades_list
        if t.market_id == p.market_id and t.status == "partial"
    }
    blockers = sorted(
        {
            f"{name}: {result}"
            for d in decisions_list
            for name, result in d.gate_results.items()
            if isinstance(result, dict) and result.get("passed") is False
        },
    )
    next_priorities = _next_priorities(
        prev_plan=prev_plan,
        decisions=decisions_list,
        predictions=predictions_list,
    )

    return CyclePlan(
        written_at=written_at,
        written_by_cycle_id=cycle_id,
        next_priorities=next_priorities,
        holds_with_rationale=holds_with_rationale,
        pending_settlements=pending_settlements,
        opportunities_deferred=opportunities_deferred,
        blockers=blockers,
    )


def _next_priorities(
    *,
    prev_plan: CyclePlan | None,
    decisions: list[Decision],
    predictions: list[Prediction],
) -> list[str]:
    """Carry forward unresolved priorities and append open hot spots."""
    carried = list(prev_plan.next_priorities) if prev_plan is not None else []
    traded = {d.market_id for d in decisions if d.action == "trade"}
    carried = [p for p in carried if p not in traded]
    high_edge_markets = sorted(
        {p.market_id for p in predictions if abs(p.edge) > HIGH_EDGE_THRESHOLD and p.market_id not in traded},
    )
    seen = set(carried)
    for m in high_edge_markets:
        if m not in seen:
            carried.append(m)
            seen.add(m)
    return carried
