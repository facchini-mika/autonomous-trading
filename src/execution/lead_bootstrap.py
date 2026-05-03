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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from execution.cycle_plan import synthesize_cycle_plan
from execution.decision_context import with_decision
from execution.notes_tool import manage_notes
from risk.sizing import propose_notional
from shared.db import get_session
from shared.logging import bind, get_logger, unbind
from shared.models import (
    CashBalance,
    CyclePlan,
    Decision,
    Lesson,
    MarketMetadata,
    Note,
    Order,
    Orderbook,
    PortfolioState,
    Position,
    Prediction,
    RiskExecutionOutput,
    RiskExecutionTask,
    ScannerReviewerOutput,
    ScannerReviewerTask,
    SizingProposal,
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

    universe_inputs = _collect_universe_inputs(adapter=adapter, factory=factory, settings=settings, now=now)
    scanner_out = scanner(
        ScannerReviewerTask(
            top_k=settings.TOP_K_MARKETS,
            cycle_clock=now.isoformat(),
            raw_markets=universe_inputs["raw_markets"],
            raw_orderbooks=universe_inputs["raw_orderbooks"],
            raw_metadata=universe_inputs["raw_metadata"],
            current_positions=universe_inputs["current_positions"],
            current_cash=universe_inputs["current_cash"],
            kill_switch_active=universe_inputs["kill_switch_active"],
            held_market_ids=universe_inputs["held_market_ids"],
            orders_in_last_hour=universe_inputs["orders_in_last_hour"],
        )
    )
    universe = scanner_out.universe
    portfolio = scanner_out.portfolio_state

    # Upsert markets + record per-cycle snapshots before any FK-dependent insert.
    _persist_universe(factory=factory, universe=universe, clock=now)

    trading_context = _collect_trading_context(factory=factory, settings=settings, now=now)
    trading_out = trading(
        TradingAgentTask(
            universe=universe,
            portfolio_state=portfolio,
            lessons=trading_context["lessons"],
            recent_notes=trading_context["recent_notes"],
            prev_cycle_plan=prev_plan,
            cycle_id=cycle_id,
            edge_threshold=settings.EDGE_THRESHOLD,
        )
    )
    predictions = list(trading_out.predictions)

    proposals = _build_sizing_proposals(predictions=predictions, portfolio=portfolio)
    risk_out = risk(
        RiskExecutionTask(
            predictions=predictions,
            portfolio_state=portfolio,
            cycle_id=cycle_id,
            proposals=proposals,
        )
    )
    decisions = list(risk_out.decisions)
    trades = list(risk_out.trades)

    # Persist predictions + decisions BEFORE order placement so paper_trades and
    # trades can satisfy the decision_id FK on insert.
    _persist_predictions_and_decisions(factory=factory, predictions=predictions, decisions=decisions)

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

    _persist_cycle_plan(factory=factory, cycle_id=cycle_id, cycle_plan=cycle_plan, prev_plan=prev_plan)

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


def _persist_universe(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    universe: Universe,
    clock: datetime,
) -> None:
    """Upsert markets from the universe and record snapshots from the orderbooks."""
    with factory() as session:
        for market in universe.markets:
            session.execute(
                text(
                    """
                    INSERT INTO markets (market_id, condition_id, slug, title, category,
                        end_date, status, resolution_source, created_at, last_seen, ambiguity_score)
                    VALUES (:market_id, :condition_id, :slug, :title, :category,
                        :end_date, :status, :resolution_source, :created_at, :last_seen, :ambiguity_score)
                    ON CONFLICT (market_id) DO UPDATE
                        SET status = EXCLUDED.status,
                            last_seen = EXCLUDED.last_seen,
                            ambiguity_score = EXCLUDED.ambiguity_score
                    """,
                ),
                {
                    "market_id": market.market_id,
                    "condition_id": market.condition_id,
                    "slug": market.slug,
                    "title": market.title,
                    "category": market.category,
                    "end_date": market.end_date,
                    "status": market.status,
                    "resolution_source": market.resolution_source,
                    "created_at": market.created_at,
                    "last_seen": market.last_seen,
                    "ambiguity_score": market.ambiguity_score,
                },
            )
        for market_id, book in universe.orderbooks.items():
            session.execute(
                text(
                    """
                    INSERT INTO market_snapshots (time, market_id, best_bid, best_ask, mid,
                        depth_bid_1pct, depth_ask_1pct, volume_24h)
                    VALUES (:time, :market_id, :best_bid, :best_ask, :mid,
                        :depth_bid_1pct, :depth_ask_1pct, :volume_24h)
                    ON CONFLICT (time, market_id) DO NOTHING
                    """,
                ),
                {
                    "time": clock,
                    "market_id": market_id,
                    "best_bid": book.best_bid,
                    "best_ask": book.best_ask,
                    "mid": book.mid,
                    "depth_bid_1pct": book.depth_bid_1pct,
                    "depth_ask_1pct": book.depth_ask_1pct,
                    "volume_24h": None,
                },
            )


def _persist_predictions_and_decisions(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    predictions: list[Prediction],
    decisions: list[Decision],
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


def _persist_cycle_plan(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    cycle_id: str,
    cycle_plan: CyclePlan,
    prev_plan: CyclePlan | None,
) -> None:
    with factory() as session:
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


def _collect_universe_inputs(
    *,
    adapter: PredictionMarketAdapter,
    factory: Callable[[], AbstractContextManager[Session]],
    settings: Settings,
    now: datetime,
) -> dict[str, object]:
    """Pre-fetch raw market data + portfolio state for the scanner-reviewer.

    Pulls up to ``settings.UNIVERSE_FETCH_LIMIT`` markets via the adapter,
    walks each one for orderbook + metadata, and reads the DB-side state
    (positions, cash, kill_switch, recent-orders count) the scanner needs to
    synthesise its filtered ``Universe`` + ``PortfolioState``.
    """
    raw_markets = adapter.get_markets(limit=settings.UNIVERSE_FETCH_LIMIT)
    raw_orderbooks: dict[str, Orderbook] = {}
    raw_metadata: dict[str, MarketMetadata] = {}
    for market in raw_markets:
        try:
            raw_orderbooks[market.market_id] = adapter.get_orderbook(market.market_id)
        except Exception:
            logger.warning("orderbook_fetch_failed", market_id=market.market_id)
            continue
        try:
            raw_metadata[market.market_id] = adapter.get_metadata(market.market_id)
        except Exception:
            logger.warning("metadata_fetch_failed", market_id=market.market_id)

    positions = _load_open_positions(factory)
    cash = _load_cash_balance(factory=factory, settings=settings, now=now)
    kill_switch = _load_kill_switch(factory)
    orders_count = _count_orders_in_last_hour(factory=factory, now=now)

    held_market_ids = sorted({p.market_id for p in positions})
    return {
        "raw_markets": raw_markets,
        "raw_orderbooks": raw_orderbooks,
        "raw_metadata": raw_metadata,
        "current_positions": positions,
        "current_cash": cash,
        "kill_switch_active": kill_switch,
        "held_market_ids": held_market_ids,
        "orders_in_last_hour": orders_count,
    }


def _collect_trading_context(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    settings: Settings,
    now: datetime,  # noqa: ARG001 — reserved for future time-window queries
) -> dict[str, object]:
    """Pre-fetch lessons + recent notes for the trading-agent prompt context."""
    lessons = _load_open_lessons(
        factory=factory,
        top_k=settings.LESSONS_TOP_K,
        lookback_days=settings.LESSONS_LOOKBACK_DAYS,
    )
    recent_notes = _load_recent_notes(factory=factory, agent_id="trading-agent")
    return {
        "lessons": lessons,
        "recent_notes": recent_notes,
    }


def _load_open_positions(
    factory: Callable[[], AbstractContextManager[Session]],
) -> list[Position]:
    with factory() as session:
        rows = session.execute(
            text(
                """
                SELECT market_id, side, size, avg_price, unrealized_pnl, realized_pnl,
                       opened_at, last_updated, status
                FROM positions
                WHERE status = 'open'
                """,
            ),
        ).all()
    return [
        Position(
            market_id=r.market_id,
            side=r.side,
            size=float(r.size),
            avg_price=float(r.avg_price),
            unrealized_pnl=float(r.unrealized_pnl) if r.unrealized_pnl is not None else 0.0,
            realized_pnl=float(r.realized_pnl) if r.realized_pnl is not None else 0.0,
            opened_at=r.opened_at,
            last_updated=r.last_updated,
            status=r.status,
        )
        for r in rows
    ]


def _load_cash_balance(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    settings: Settings,
    now: datetime,
) -> CashBalance:
    """Compute current cash.

    Paper mode: ``PAPER_STARTING_CASH_USD`` minus open paper-trade notional.
    Real mode: same baseline for now (Phase-7 will pull live USDC); paper-
    mode is the only certified path until the operator flips
    ``MAX_CAPITAL_EUR``.
    """
    table = "paper_trades" if settings.TRADING_MODE == "paper" else "trades"
    with factory() as session:
        row = session.execute(
            text(
                f"""
                SELECT COALESCE(SUM(notional_usd), 0) AS spent
                FROM {table}
                WHERE status IN ('open', 'pending', 'partial')
                """,
            ),
        ).first()
    spent = float(row.spent) if row is not None else 0.0
    total = settings.PAPER_STARTING_CASH_USD
    return CashBalance(
        total_usd=total,
        available=max(total - spent, 0.0),
        reserved_for_orders=spent,
        timestamp=now,
    )


def _load_kill_switch(
    factory: Callable[[], AbstractContextManager[Session]],
) -> bool:
    with factory() as session:
        row = session.execute(
            text("SELECT value FROM system_state WHERE key = 'kill_switch'"),
        ).first()
    if row is None:
        return False
    value = row.value
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        active = value.get("active") or value.get("value")
        return bool(active)
    return bool(value)


def _count_orders_in_last_hour(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    now: datetime,
) -> int:
    cutoff = now - timedelta(hours=1)
    with factory() as session:
        row = session.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM trades WHERE created_at > :cutoff)
                  + (SELECT COUNT(*) FROM paper_trades WHERE created_at > :cutoff)
                AS total
                """,
            ),
            {"cutoff": cutoff},
        ).first()
    return int(row.total) if row is not None else 0


def _load_open_lessons(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    top_k: int,
    lookback_days: int,
) -> list[Lesson]:
    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    with factory() as session:
        rows = session.execute(
            text(
                """
                SELECT id, source_agent_id, trigger_event_id, market_id, observation,
                       hypothesis, action_taken, outcome, status, parent_pattern_id,
                       tags, created_at
                FROM lessons
                WHERE status = 'open' AND created_at > :cutoff
                ORDER BY created_at DESC
                LIMIT :top_k
                """,
            ),
            {"cutoff": cutoff, "top_k": top_k},
        ).all()
    return [
        Lesson(
            id=r.id,
            source_agent_id=r.source_agent_id,
            trigger_event_id=r.trigger_event_id,
            market_id=r.market_id,
            observation=r.observation,
            hypothesis=r.hypothesis,
            action_taken=r.action_taken,
            outcome=r.outcome,
            status=r.status,
            parent_pattern_id=r.parent_pattern_id,
            tags=list(r.tags or []),
            created_at=r.created_at,
        )
        for r in rows
    ]


def _load_recent_notes(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    agent_id: str,
) -> list[Note]:
    raw = manage_notes(action="read", agent_id=agent_id, session_factory=factory)
    notes: list[Note] = []
    for item in raw:
        note_id = item.get("id")
        last_accessed = item.get("last_accessed")
        created_at = item.get("created_at")
        if not isinstance(note_id, str):
            continue
        if not isinstance(last_accessed, datetime) or not isinstance(created_at, datetime):
            continue
        body = item.get("body", "")
        tags_raw = item.get("tags", [])
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        try:
            uuid_obj = UUID(note_id)
        except ValueError:
            continue
        notes.append(
            Note(
                id=uuid_obj,
                agent_id=agent_id,
                body=str(body),
                tags=tags,
                last_accessed=last_accessed,
                created_at=created_at,
            )
        )
    return notes


def _build_sizing_proposals(
    *,
    predictions: list[Prediction],
    portfolio: PortfolioState,
) -> list[SizingProposal]:
    """Compute one ``SizingProposal`` per prediction with non-zero notional.

    ``q_market`` is recovered from the prediction itself: since the
    trading-agent computes ``edge = p_yes - q_market`` against the
    side-relevant orderbook quote, we can invert. Predictions with
    out-of-range derived ``q_market`` or zero notional are dropped.
    """
    out: list[SizingProposal] = []
    equity = portfolio.equity
    for prediction in predictions:
        q_market = prediction.p_yes - prediction.edge
        if not (0.0 < q_market < 1.0):
            continue
        notional = propose_notional(
            p_yes=prediction.p_yes,
            q_market=q_market,
            equity=equity,
        )
        if notional <= 0.0:
            continue
        side = "yes" if prediction.edge > 0 else "no"
        out.append(
            SizingProposal(
                market_id=prediction.market_id,
                prediction_id=prediction.id,
                proposed_notional_usd=notional,
                side=side,
                q_market=q_market,
            )
        )
    return out


def _team_cleanup(*, cycle_id: str) -> None:
    """Marker that the Lead has finished the cycle. Asserted by the Stop hook."""
    logger.info("clean up the team", cycle_id=cycle_id)
