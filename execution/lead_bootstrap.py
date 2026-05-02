"""Lead orchestration for one trading cycle.

The Lead is a deterministic coordinator (no LLM). It loads the previous
CyclePlan, dispatches three sub-tasks (scanner-reviewer → trading-agent →
risk-execution), persists the resulting Pydantic artifacts, writes the new
CyclePlan, and supersedes the prior one.

Sub-agent execution is injected as callables so tests can pass synchronous
fakes; production wires them to real Claude Code subagent invocations
(handled outside Phase 4d).
"""

from __future__ import annotations

import json
from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from execution.cycle_plan import synthesize_cycle_plan
from execution.decision_context import with_decision
from shared.db import get_session
from shared.logging import bind, get_logger, unbind
from shared.models import (
    CyclePlan,
    Decision,
    Order,
    PortfolioState,
    Prediction,
    RiskExecutionOutput,
    RiskExecutionTask,
    ScannerReviewerOutput,
    ScannerReviewerTask,
    Trade,
    TradingAgentOutput,
    TradingAgentTask,
    Universe,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

    from shared.adapters.prediction_market import PredictionMarketAdapter
    from shared.config.settings import Settings

logger = get_logger(__name__)


@dataclass(frozen=True)
class CycleArtifacts:
    cycle_id: str
    universe: Universe
    portfolio: PortfolioState
    predictions: list[Prediction]
    decisions: list[Decision]
    trades: list[Trade]
    cycle_plan: CyclePlan


def bootstrap_team(
    *,
    settings: Settings,
    adapter: PredictionMarketAdapter,
    scanner: Callable[[ScannerReviewerTask], ScannerReviewerOutput],
    trading: Callable[[TradingAgentTask], TradingAgentOutput],
    risk: Callable[[RiskExecutionTask], RiskExecutionOutput],
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CycleArtifacts:
    """Run one trading cycle end-to-end against the given adapter."""
    factory = session_factory or _default_factory
    now = (clock or _utcnow)()
    cycle_id = f"cycle-{int(now.timestamp())}"
    bind(cycle_id=cycle_id)
    logger.info("cycle_starting", adapter=type(adapter).__name__, mode=settings.TRADING_MODE)

    prev_plan = _load_prev_plan(factory)

    scanner_out = scanner(ScannerReviewerTask(top_k=settings.TOP_K_MARKETS, cycle_clock=now.isoformat()))
    universe = scanner_out.universe
    portfolio = scanner_out.portfolio_state

    trading_out = trading(TradingAgentTask(universe=universe, portfolio_state=portfolio))
    predictions = list(trading_out.predictions)

    risk_out = risk(RiskExecutionTask(predictions=predictions, portfolio_state=portfolio))
    decisions = list(risk_out.decisions)
    trades = list(risk_out.trades)

    for decision in decisions:
        if decision.action != "trade":
            continue
        order = _decision_to_order(decision, cycle_id=cycle_id)
        if order is None:
            continue
        with with_decision(decision.id):
            adapter.place_order(order)

    cycle_plan = synthesize_cycle_plan(
        cycle_id=cycle_id,
        predictions=predictions,
        decisions=decisions,
        trades=trades,
        positions=[],
        prev_plan=prev_plan,
        clock=now,
    )

    _persist(
        factory=factory,
        cycle_id=cycle_id,
        predictions=predictions,
        decisions=decisions,
        cycle_plan=cycle_plan,
        prev_plan=prev_plan,
    )

    _team_cleanup(cycle_id=cycle_id)
    unbind("cycle_id")

    return CycleArtifacts(
        cycle_id=cycle_id,
        universe=universe,
        portfolio=portfolio,
        predictions=predictions,
        decisions=decisions,
        trades=trades,
        cycle_plan=cycle_plan,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("trading_cycle")


def _decision_to_order(decision: Decision, *, cycle_id: str) -> Order | None:
    edge_sign = decision.p_consensus - decision.q_market
    side = "yes" if edge_sign > 0 else "no"
    notional = _decision_notional(decision)
    if notional <= 0:
        return None
    price = decision.q_market if side == "yes" else (1.0 - decision.q_market)
    if price <= 0 or price >= 1:
        return None
    size = notional / price
    return Order(
        market_id=decision.market_id,
        side=side,
        size=size,
        price=price,
        notional_usd=notional,
        idempotency_key=f"{cycle_id}:{decision.id}",
    )


def _decision_notional(decision: Decision) -> float:
    clipped = decision.gate_results.get("clipped_notional")
    if isinstance(clipped, (int, float)) and clipped > 0:
        return float(clipped)
    base = decision.gate_results.get("notional_usd")
    if isinstance(base, (int, float)) and base > 0:
        return float(base)
    return 0.0


def _load_prev_plan(
    factory: Callable[[], AbstractContextManager[Session]],
) -> CyclePlan | None:
    with factory() as session:
        row = session.execute(
            text(
                """
                SELECT id, written_at, written_by_cycle_id, next_priorities,
                       holds_with_rationale, pending_settlements,
                       opportunities_deferred, blockers
                FROM cycle_plan
                WHERE superseded_at IS NULL
                ORDER BY written_at DESC
                LIMIT 1
                """,
            ),
        ).first()
    if row is None:
        return None
    return CyclePlan(
        id=row.id,
        written_at=row.written_at,
        written_by_cycle_id=row.written_by_cycle_id,
        next_priorities=list(row.next_priorities or []),
        holds_with_rationale=dict(row.holds_with_rationale or {}),
        pending_settlements=dict(row.pending_settlements or {}),
        opportunities_deferred=list(row.opportunities_deferred or []),
        blockers=list(row.blockers or []),
    )


def _persist(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    cycle_id: str,
    predictions: list[Prediction],
    decisions: list[Decision],
    cycle_plan: CyclePlan,
    prev_plan: CyclePlan | None,
) -> None:
    with factory() as session:
        for p in predictions:
            session.execute(
                text(
                    """
                    INSERT INTO predictions (id, market_id, agent_id, p_raw, inference_log,
                        latency_ms, outcome, realized_pnl, created_at)
                    VALUES (:id, :market_id, :agent_id, :p_raw, CAST(:log AS jsonb),
                        :latency_ms, :outcome, :realized_pnl, :created_at)
                    """,
                ),
                {
                    "id": str(p.id),
                    "market_id": p.market_id,
                    "agent_id": p.agent_id,
                    "p_raw": p.p_yes,
                    "log": json.dumps(p.inference_log),
                    "latency_ms": p.latency_ms,
                    "outcome": p.outcome,
                    "realized_pnl": p.realized_pnl,
                    "created_at": p.created_at,
                },
            )
        for d in decisions:
            session.execute(
                text(
                    """
                    INSERT INTO decisions (id, cycle_id, market_id, p_consensus, q_market,
                        edge, gate_results, action, rationale, created_at)
                    VALUES (:id, :cycle_id, :market_id, :p, :q, :edge, CAST(:gates AS jsonb),
                        :action, :rationale, :created_at)
                    """,
                ),
                {
                    "id": str(d.id),
                    "cycle_id": d.cycle_id,
                    "market_id": d.market_id,
                    "p": d.p_consensus,
                    "q": d.q_market,
                    "edge": d.edge,
                    "gates": json.dumps(d.gate_results),
                    "action": d.action,
                    "rationale": d.rationale,
                    "created_at": d.created_at,
                },
            )
        if prev_plan is not None:
            session.execute(
                text("UPDATE cycle_plan SET superseded_at = NOW() WHERE id = :id"),
                {"id": str(prev_plan.id)},
            )
        session.execute(
            text(
                """
                INSERT INTO cycle_plan (id, written_at, written_by_cycle_id, next_priorities,
                    holds_with_rationale, pending_settlements, opportunities_deferred, blockers)
                VALUES (:id, :written_at, :cycle_id, :next, CAST(:holds AS jsonb),
                    CAST(:pending AS jsonb), :deferred, :blockers)
                """,
            ),
            {
                "id": str(cycle_plan.id),
                "written_at": cycle_plan.written_at,
                "cycle_id": cycle_id,
                "next": cycle_plan.next_priorities,
                "holds": json.dumps(cycle_plan.holds_with_rationale),
                "pending": json.dumps(cycle_plan.pending_settlements),
                "deferred": cycle_plan.opportunities_deferred,
                "blockers": cycle_plan.blockers,
            },
        )


def _team_cleanup(*, cycle_id: str) -> None:
    """Marker that the Lead has finished the cycle. Asserted by the Stop hook."""
    logger.info("clean up the team", cycle_id=cycle_id)
