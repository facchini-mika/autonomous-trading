"""Tests for deterministic cycle-plan synthesis."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from execution.cycle_plan import synthesize_cycle_plan
from shared.models import CyclePlan, Decision, Prediction


def _decision(market: str, action: str, **gates: object) -> Decision:
    return Decision(
        cycle_id="cycle-1",
        market_id=market,
        p_consensus=0.6,
        q_market=0.5,
        edge=0.1,
        gate_results=gates,
        action=action,
        rationale=f"action={action}",
        created_at=datetime(2026, 5, 2, tzinfo=UTC),
    )


def _prediction(market: str, edge: float) -> Prediction:
    return Prediction(
        market_id=market,
        agent_id="trading-agent",
        p_yes=0.5 + edge,
        reasoning="x",
        edge=edge,
        latency_ms=10,
        created_at=datetime(2026, 5, 2, tzinfo=UTC),
    )


def test_holds_with_rationale_extracted() -> None:
    plan = synthesize_cycle_plan(
        cycle_id="c1",
        predictions=[],
        decisions=[_decision("0xa", "hold")],
        trades=[],
        positions=[],
        prev_plan=None,
    )
    assert plan.holds_with_rationale == {"0xa": "action=hold"}


def test_skip_decisions_become_opportunities_deferred() -> None:
    plan = synthesize_cycle_plan(
        cycle_id="c1",
        predictions=[],
        decisions=[_decision("0xa", "skip"), _decision("0xb", "skip")],
        trades=[],
        positions=[],
        prev_plan=None,
    )
    assert plan.opportunities_deferred == ["0xa", "0xb"]


def test_blockers_extracted_from_failed_gates() -> None:
    plan = synthesize_cycle_plan(
        cycle_id="c1",
        predictions=[],
        decisions=[_decision("0xa", "skip", capital_gate={"passed": False, "reason": "max_cap=0"})],
        trades=[],
        positions=[],
        prev_plan=None,
    )
    assert any("capital_gate" in b for b in plan.blockers)


def test_high_edge_predictions_become_priorities_when_not_traded() -> None:
    plan = synthesize_cycle_plan(
        cycle_id="c1",
        predictions=[_prediction("0xnew", 0.20), _prediction("0xboring", 0.02)],
        decisions=[],
        trades=[],
        positions=[],
        prev_plan=None,
    )
    assert "0xnew" in plan.next_priorities
    assert "0xboring" not in plan.next_priorities


def test_traded_priorities_dropped_from_carry_forward() -> None:
    prev = CyclePlan(
        id=uuid4(),
        written_at=datetime(2026, 5, 1, tzinfo=UTC),
        written_by_cycle_id="c0",
        next_priorities=["0xa", "0xb"],
    )
    plan = synthesize_cycle_plan(
        cycle_id="c1",
        predictions=[],
        decisions=[_decision("0xa", "trade")],
        trades=[],
        positions=[],
        prev_plan=prev,
    )
    assert "0xa" not in plan.next_priorities
    assert "0xb" in plan.next_priorities


def test_synthesis_is_deterministic() -> None:
    inputs = {
        "cycle_id": "c1",
        "predictions": [_prediction("0xa", 0.20)],
        "decisions": [_decision("0xb", "hold")],
        "trades": [],
        "positions": [],
        "prev_plan": None,
        "clock": datetime(2026, 5, 2, tzinfo=UTC),
    }
    p1 = synthesize_cycle_plan(**inputs)  # type: ignore[arg-type]
    p2 = synthesize_cycle_plan(**inputs)  # type: ignore[arg-type]
    p1_dump = p1.model_dump(exclude={"id"})
    p2_dump = p2.model_dump(exclude={"id"})
    assert p1_dump == p2_dump
