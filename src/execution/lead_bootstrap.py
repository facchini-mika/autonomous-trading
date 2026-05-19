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
from uuid import UUID, uuid4

from sqlalchemy import text

from execution.cycle_plan import synthesize_cycle_plan
from execution.decision_context import with_decision
from execution.notes_tool import manage_notes
from execution.subagent_runner import _INSERT_SUBAGENT_RUN, SubagentBudgetError
from risk.sizing import propose_notional
from shared.db import get_session
from shared.logging import bind, get_logger, unbind
from shared.models import (
    CashBalance,
    CyclePlan,
    Decision,
    Lesson,
    Market,
    MarketMetadata,
    Note,
    Order,
    Orderbook,
    OrderSide,
    PortfolioState,
    Position,
    Prediction,
    RiskExecutionOutput,
    RiskExecutionTask,
    ScannerReviewerOutput,
    ScannerReviewerTask,
    ScannerThresholds,
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


class CycleAbortedError(RuntimeError):
    """Raised when a cycle terminates early due to a recoverable runtime condition.

    The current trigger is API-side budget exhaustion (out-of-credits,
    quota, rate-limit) bubbled up from a subagent. The Lead catches the
    underlying error, refuses to persist partial decisions/trades, and
    raises this so the caller can exit cleanly without crashing the
    cron host.
    """

    def __init__(self, *, reason: str, cycle_id: str, stage: str) -> None:
        self.reason = reason
        self.cycle_id = cycle_id
        self.stage = stage
        super().__init__(f"cycle {cycle_id} aborted at {stage}: {reason}")


def bootstrap_team(  # noqa: PLR0915 — single-function cycle orchestrator by design
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

    _log_orphan_attempts(factory=factory, max_age_minutes=settings.ORPHAN_ATTEMPT_WARN_AFTER_MIN)

    prev_plan = _load_prev_plan(factory)

    universe_inputs = _collect_universe_inputs(adapter=adapter, factory=factory, settings=settings, now=now)
    thresholds = ScannerThresholds(
        min_depth_1pct_usd=settings.MIN_DEPTH_1PCT_USD,
        max_spread=settings.MAX_SPREAD,
        min_ttr_hours=settings.MIN_TIME_TO_RESOLUTION_HOURS,
        max_ttr_days=settings.MAX_TIME_TO_RESOLUTION_DAYS,
        soon_resolve_threshold_days=settings.SOON_RESOLVE_THRESHOLD_DAYS,
        soon_resolve_boost_multiplier=settings.SOON_RESOLVE_BOOST_MULTIPLIER,
    )
    scanner_task = ScannerReviewerTask(
        top_k=settings.TOP_K_MARKETS,
        cycle_id=cycle_id,
        cycle_clock=now.isoformat(),
        raw_markets=universe_inputs["raw_markets"],
        raw_orderbooks=universe_inputs["raw_orderbooks"],
        raw_metadata=universe_inputs["raw_metadata"],
        current_positions=universe_inputs["current_positions"],
        current_cash=universe_inputs["current_cash"],
        kill_switch_active=universe_inputs["kill_switch_active"],
        held_market_ids=universe_inputs["held_market_ids"],
        orders_in_last_hour=universe_inputs["orders_in_last_hour"],
        thresholds=thresholds,
    )
    if settings.UNIVERSE_FETCH_LIMIT <= settings.TOP_K_MARKETS:
        # Identity / sub-identity filter — Lead's Python implementation mirrors
        # the scanner doctrine 1:1 and skips the LLM call. ~$0.71/cycle saved
        # plus eliminates the 13-24k output-token wall-time tail.
        bypass_started_at = _utcnow()
        scanner_out = _python_scanner(scanner_task, clock=now)
        _persist_scanner_bypass_audit(
            factory=factory,
            cycle_id=cycle_id,
            task=scanner_task,
            output=scanner_out,
            started_at=bypass_started_at,
            finished_at=_utcnow(),
        )
    else:
        try:
            scanner_out = scanner(scanner_task)
        except SubagentBudgetError as exc:
            _log_budget_abort(cycle_id=cycle_id, stage="scanner-reviewer", exc=exc)
            unbind("cycle_id")
            raise CycleAbortedError(reason=str(exc), cycle_id=cycle_id, stage="scanner-reviewer") from exc
    portfolio = scanner_out.portfolio_state

    # Defensive post-filter. The scanner is an LLM and was observed in cycle-7
    # to violate its threshold contract under universe-pressure (picked
    # markets with TTR=239d despite max_ttr_days=14). The trading-agent
    # would skip web_search for any such pick, so we filter them here.
    held_market_ids = universe_inputs["held_market_ids"]
    assert isinstance(held_market_ids, list)  # noqa: S101 — _collect_universe_inputs always sets this.
    universe = _enforce_universe_invariants(
        universe=scanner_out.universe,
        thresholds=thresholds,
        held_market_ids=set(held_market_ids),
        clock=now,
    )

    # Upsert markets + record per-cycle snapshots before any FK-dependent insert.
    _persist_universe(factory=factory, universe=universe, clock=now)

    trading_context = _collect_trading_context(factory=factory, settings=settings, now=now)
    try:
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
    except SubagentBudgetError as exc:
        _log_budget_abort(cycle_id=cycle_id, stage="trading-agent", exc=exc)
        unbind("cycle_id")
        raise CycleAbortedError(reason=str(exc), cycle_id=cycle_id, stage="trading-agent") from exc
    predictions = [p.model_copy(update={"cycle_id": cycle_id}) for p in trading_out.predictions]

    proposals = _build_sizing_proposals(predictions=predictions, portfolio=portfolio, adapter=adapter)
    try:
        risk_out = risk(
            RiskExecutionTask(
                predictions=predictions,
                portfolio_state=portfolio,
                cycle_id=cycle_id,
                proposals=proposals,
            )
        )
    except SubagentBudgetError as exc:
        _log_budget_abort(cycle_id=cycle_id, stage="risk-execution", exc=exc)
        unbind("cycle_id")
        raise CycleAbortedError(reason=str(exc), cycle_id=cycle_id, stage="risk-execution") from exc
    decisions = list(risk_out.decisions)

    # Persist predictions + decisions BEFORE order placement so paper_trades and
    # trades can satisfy the decision_id FK on insert.
    _persist_predictions_and_decisions(factory=factory, predictions=predictions, decisions=decisions)

    trades = _place_orders_and_collect_trades(
        adapter=adapter,
        decisions=decisions,
        cycle_id=cycle_id,
        now=now,
    )
    _persist_agent_notes(factory=factory, predictions=predictions)

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
    _persist_equity_snapshot(
        factory=factory,
        cycle_id=cycle_id,
        portfolio=portfolio,
        started_at=now,
        finished_at=_utcnow(),
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


def _enforce_universe_invariants(
    *,
    universe: Universe,
    thresholds: ScannerThresholds,
    held_market_ids: set[str],
    clock: datetime,
) -> Universe:
    """Drop scanner-picked markets that violate the deterministic filter contract.

    The scanner is an LLM and was observed in cycle-7 to break its
    `max_ttr_days` constraint under universe-pressure (picked markets at
    TTR=239d despite max_ttr_days=14). We enforce the deterministic side
    of the filter policy here so the trading-agent never sees a
    non-compliant market. Held markets are always preserved regardless
    of any threshold — operators must keep visibility on positions under
    management.

    Mirrors the doctrine in ``research/prompts/scanner_reviewer.md``
    sections "Filtering policy" steps 1-6 (held bypass, status, TTR
    window, liquidity floor, spread ceiling, ambiguity). Ranking,
    soft-boost, and top_k truncation remain the scanner's job.

    Per-drop ``scanner_violation`` warnings give forensics; one summary
    ``scanner_post_filter_done`` info log carries the kept/dropped split.
    """
    min_ttr = timedelta(hours=thresholds.min_ttr_hours)
    max_ttr = timedelta(days=thresholds.max_ttr_days)
    kept_markets: list[Market] = []
    dropped_reasons: dict[str, int] = {}

    for market in universe.markets:
        if market.market_id in held_market_ids:
            kept_markets.append(market)
            continue
        reason = _universe_drop_reason(
            market=market,
            orderbook=universe.orderbooks.get(market.market_id),
            min_ttr=min_ttr,
            max_ttr=max_ttr,
            min_depth_1pct_usd=thresholds.min_depth_1pct_usd,
            max_spread=thresholds.max_spread,
            clock=clock,
        )
        if reason is None:
            kept_markets.append(market)
            continue
        dropped_reasons[reason] = dropped_reasons.get(reason, 0) + 1
        logger.warning(
            "scanner_violation",
            market_id=market.market_id,
            reason=reason,
        )

    kept_market_ids = {m.market_id for m in kept_markets}
    kept_orderbooks = {mid: ob for mid, ob in universe.orderbooks.items() if mid in kept_market_ids}
    logger.info(
        "scanner_post_filter_done",
        kept_count=len(kept_markets),
        dropped_count=len(universe.markets) - len(kept_markets),
        dropped_reasons=dropped_reasons,
    )
    return Universe(
        markets=kept_markets,
        orderbooks=kept_orderbooks,
        timestamp=universe.timestamp,
    )


_AMBIGUITY_CEILING: float = 0.6


def _universe_drop_reason(
    *,
    market: Market,
    orderbook: Orderbook | None,
    min_ttr: timedelta,
    max_ttr: timedelta,
    min_depth_1pct_usd: float,
    max_spread: float,
    clock: datetime,
) -> str | None:
    """Return a short reason string if the market violates a filter, else None.

    Iterates a fixed list of (predicate, reason) pairs so adding a new
    invariant is one line and the function stays under the return-count
    threshold (Ruff PLR0911).
    """
    if orderbook is None:
        return "orderbook_missing"
    ttr = market.end_date - clock
    spread = orderbook.best_ask - orderbook.best_bid
    min_depth = min(orderbook.depth_bid_1pct, orderbook.depth_ask_1pct)
    checks: list[tuple[bool, str]] = [
        (market.status != "open", "status_not_open"),
        (ttr < min_ttr, "ttr_below_min"),
        (ttr > max_ttr, "ttr_above_max"),
        (min_depth < min_depth_1pct_usd, "liquidity_below_floor"),
        (spread > max_spread, "spread_above_ceiling"),
        (
            market.ambiguity_score is not None and market.ambiguity_score > _AMBIGUITY_CEILING,
            "ambiguity_above_ceiling",
        ),
    ]
    for failed, reason in checks:
        if failed:
            return reason
    return None


def _python_scanner(task: ScannerReviewerTask, *, clock: datetime) -> ScannerReviewerOutput:
    """Deterministic Python equivalent of the scanner-reviewer subagent.

    Mirrors ``research/prompts/scanner_reviewer.md §54-124`` 1:1 — filter,
    rank by Soft-Boost score, truncate to ``top_k``, then assemble
    ``PortfolioState``. Used when ``UNIVERSE_FETCH_LIMIT <= TOP_K_MARKETS``;
    the LLM scanner has no real filtering job in that configuration and the
    bypass eliminates ~$0.71/cycle plus the 13-24k output-token wall-time
    tail observed on Opus 4.7.
    """
    held_ids = set(task.held_market_ids)
    min_ttr = timedelta(hours=task.thresholds.min_ttr_hours)
    max_ttr = timedelta(days=task.thresholds.max_ttr_days)

    survivors: list[Market] = []
    for market in task.raw_markets:
        if market.market_id in held_ids:
            survivors.append(market)
            continue
        orderbook = task.raw_orderbooks.get(market.market_id)
        if orderbook is None:
            continue
        reason = _universe_drop_reason(
            market=market,
            orderbook=orderbook,
            min_ttr=min_ttr,
            max_ttr=max_ttr,
            min_depth_1pct_usd=task.thresholds.min_depth_1pct_usd,
            max_spread=task.thresholds.max_spread,
            clock=clock,
        )
        if reason is not None:
            continue
        metadata = task.raw_metadata.get(market.market_id)
        if metadata is not None and metadata.dispute_history:
            continue
        survivors.append(market)

    boost_threshold = timedelta(days=task.thresholds.soon_resolve_threshold_days)
    boost_multiplier = task.thresholds.soon_resolve_boost_multiplier

    def _rank_key(market: Market) -> tuple[float, datetime, str]:
        orderbook = task.raw_orderbooks.get(market.market_id)
        if orderbook is None:
            score = 0.0
        else:
            liquidity = min(orderbook.depth_bid_1pct, orderbook.depth_ask_1pct)
            ttr = market.end_date - clock
            boost = boost_multiplier if ttr < boost_threshold else 1.0
            score = liquidity * boost
        # Negative score for descending sort; tie-break by earlier end_date, then market_id.
        return (-score, market.end_date, market.market_id)

    held_markets = [m for m in survivors if m.market_id in held_ids]
    other_markets = sorted([m for m in survivors if m.market_id not in held_ids], key=_rank_key)
    ranked = held_markets + other_markets
    truncated = ranked[: task.top_k]

    timestamp = datetime.fromisoformat(task.cycle_clock)
    universe = Universe(
        markets=truncated,
        orderbooks={
            m.market_id: task.raw_orderbooks[m.market_id] for m in truncated if m.market_id in task.raw_orderbooks
        },
        timestamp=timestamp,
    )
    portfolio = _assemble_portfolio_state(task=task, timestamp=timestamp)

    logger.info(
        "scanner_python_bypass_done",
        kept_count=len(truncated),
        candidate_count=len(task.raw_markets),
        held_count=len(held_markets),
    )
    return ScannerReviewerOutput(universe=universe, portfolio_state=portfolio)


def _assemble_portfolio_state(*, task: ScannerReviewerTask, timestamp: datetime) -> PortfolioState:
    """Doctrine §104-124 — pure arithmetic from cash, positions, orderbooks."""
    positions = list(task.current_positions)
    gross_exposure = sum(p.size * p.avg_price for p in positions)
    unrealized = 0.0
    for position in positions:
        orderbook = task.raw_orderbooks.get(position.market_id)
        if orderbook is None:
            continue
        if position.side == "yes":
            unrealized += (orderbook.best_bid - position.avg_price) * position.size
        else:
            unrealized += ((1.0 - orderbook.best_ask) - position.avg_price) * position.size
    realized = sum(p.realized_pnl for p in positions)
    return PortfolioState(
        cash=task.current_cash,
        positions=positions,
        gross_exposure_usd=gross_exposure,
        unrealized_pnl=unrealized,
        realized_pnl=realized,
        equity=task.current_cash.total_usd + unrealized + realized,
        cycle_notional_opened=0.0,
        kill_switch_active=task.kill_switch_active,
        orders_in_last_hour=task.orders_in_last_hour,
        timestamp=timestamp,
    )


def _persist_scanner_bypass_audit(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    cycle_id: str,
    task: ScannerReviewerTask,
    output: ScannerReviewerOutput,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Best-effort audit row mirroring the LLM-path ``subagent_runs`` shape.

    Tier-1 evaluation, Prometheus, and cycle-completeness queries scan
    ``subagent_runs`` for one row per (cycle_id, agent_name). Skipping the row
    on bypass would create a silent gap; instead we write the same columns
    with ``cost_usd=0``, ``prompt_sha='python-bypass'``, no LLM ``usage``.
    Wrapped in try/except so an audit failure cannot poison a healthy cycle.
    """
    latency_ms = max(int((finished_at - started_at).total_seconds() * 1000), 0)
    try:
        with factory() as session:
            session.execute(
                _INSERT_SUBAGENT_RUN,
                {
                    "cycle_id": cycle_id,
                    "agent_name": "scanner-reviewer",
                    "run_id": str(uuid4()),
                    "correlation_id": None,
                    "prompt_sha": "python-bypass",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "latency_ms": latency_ms,
                    "cost_usd": 0.0,
                    "usage": None,
                    "task_payload": task.model_dump_json(),
                    "raw_stdout": None,
                    "envelope": output.model_dump_json(),
                    "error": None,
                    "error_class": None,
                },
            )
    except Exception:
        logger.warning(
            "audit_persist_failed",
            agent="scanner-reviewer",
            cycle_id=cycle_id,
            exc_info=True,
        )


def _log_budget_abort(*, cycle_id: str, stage: str, exc: SubagentBudgetError) -> None:
    logger.warning(
        "cycle_aborted_budget",
        cycle_id=cycle_id,
        stage=stage,
        reason=str(exc),
    )


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("trading_cycle")


def _persist_equity_snapshot(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    cycle_id: str,
    portfolio: PortfolioState,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Append one row to ``equity_snapshots`` at cycle close.

    The peak is computed in-SQL via ``GREATEST(prev_max, :equity)`` so we
    keep this single insert atomic and avoid a read-then-write race with
    a concurrent cycle (which cannot exist today — single-operator, single
    cron — but the SQL is the same length and is cheap insurance).

    ``portfolio.equity`` is the snapshot taken by the scanner at cycle
    *start*. Re-fetching after order placement would give a fresher number
    in paper mode (paper trades fill instantly) but the cycle cadence is
    12 minutes — the lag is bounded and the numbers stay self-consistent
    series-wide. Documented for the operator who reads the gauge.
    """
    duration_seconds = max((finished_at - started_at).total_seconds(), 0.0)
    with factory() as session:
        session.execute(
            text(
                """
                INSERT INTO equity_snapshots (
                    time, cycle_id, equity_usd, peak_equity_usd,
                    gross_exposure_usd, duration_seconds
                )
                VALUES (
                    :time, :cycle_id, :equity,
                    GREATEST(
                        COALESCE((SELECT MAX(peak_equity_usd) FROM equity_snapshots), :equity),
                        :equity
                    ),
                    :gross_exposure, :duration
                )
                """,
            ),
            {
                "time": finished_at,
                "cycle_id": cycle_id,
                "equity": portfolio.equity,
                "gross_exposure": portfolio.gross_exposure_usd,
                "duration": duration_seconds,
            },
        )


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
    if decision.action == "trade":
        # The agent declared a trade-action but did not supply the top-level
        # notional keys the Lead reads to size it. Order is silently skipped
        # downstream; this log gives drift detection so we do not lose another
        # cycle to a doctrine-vs-Lead schema mismatch unnoticed.
        logger.warning(
            "decision_notional_missing",
            market_id=decision.market_id,
            cycle_id=decision.cycle_id,
            action=decision.action,
            gate_results_keys=sorted(decision.gate_results.keys()),
        )
    return 0.0


def _log_orphan_attempts(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    max_age_minutes: int,
) -> None:
    """Surface ``order_attempts`` rows stuck in ``status='pending'``.

    A pending row older than ``max_age_minutes`` means a prior process
    crashed between ``POST /order`` and the local write that finalises
    the row. Resolving them requires reading the broker side, which is
    deliberately out of scope here (Plan: "Nur loggen, nicht agieren").
    We log a warning per orphan so the operator can reconcile manually
    via Polymarket's UI; ``orphan_order_attempts_total`` exposes the
    same signal to Prometheus.
    """
    try:
        with factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT idempotency_key, cycle_id, decision_id, created_at
                    FROM order_attempts
                    WHERE status = 'pending'
                      AND created_at < now() - make_interval(mins => :max_age_minutes)
                    ORDER BY created_at ASC
                    """,
                ),
                {"max_age_minutes": max_age_minutes},
            ).all()
    except Exception as exc:
        logger.warning("orphan_attempts_query_failed", error=str(exc))
        return
    for row in rows:
        logger.warning(
            "orphan_order_attempt",
            idempotency_key=row.idempotency_key,
            cycle_id=row.cycle_id,
            decision_id=row.decision_id,
            created_at=row.created_at.isoformat() if row.created_at else None,
            max_age_minutes=max_age_minutes,
        )


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
                        latency_ms, cycle_id, outcome, realized_pnl, created_at)
                    VALUES (:id, :market_id, :agent_id, :p_raw, CAST(:log AS jsonb),
                        :latency_ms, :cycle_id, :outcome, :realized_pnl, :created_at)
                    """,
                ),
                {
                    "id": str(p.id),
                    "market_id": p.market_id,
                    "agent_id": p.agent_id,
                    "p_raw": p.p_yes,
                    "log": json.dumps(p.inference_log),
                    "latency_ms": p.latency_ms,
                    "cycle_id": p.cycle_id,
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
                VALUES (:id, :written_at, :cycle_id, CAST(:next AS jsonb),
                    CAST(:holds AS jsonb), CAST(:pending AS jsonb),
                    CAST(:deferred AS jsonb), CAST(:blockers AS jsonb))
                """,
            ),
            {
                "id": str(cycle_plan.id),
                "written_at": cycle_plan.written_at,
                "cycle_id": cycle_id,
                "next": json.dumps(cycle_plan.next_priorities),
                "holds": json.dumps(cycle_plan.holds_with_rationale),
                "pending": json.dumps(cycle_plan.pending_settlements),
                "deferred": json.dumps(cycle_plan.opportunities_deferred),
                "blockers": json.dumps(cycle_plan.blockers),
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


def _place_orders_and_collect_trades(
    *,
    adapter: PredictionMarketAdapter,
    decisions: list[Decision],
    cycle_id: str,
    now: datetime,
) -> list[Trade]:
    """Submit each ``trade`` decision through the adapter and collect Trade rows.

    The adapter (Polymarket-real or PaperTrading wrapper) handles
    venue-side persistence (``paper_trades`` table for paper-mode). We
    additionally build a Pydantic ``Trade`` per attempt so the
    ``CycleArtifacts`` and downstream ``cycle_plan`` synthesis see the
    actual placed orders rather than the empty list the risk-execution
    agent returns.
    """
    trades: list[Trade] = []
    for decision in decisions:
        if decision.action != "trade":
            continue
        order = _decision_to_order(decision, cycle_id=cycle_id)
        if order is None:
            continue
        with with_decision(decision.id):
            try:
                result = adapter.place_order(order)
            except Exception as exc:
                logger.warning(
                    "place_order_failed",
                    decision_id=str(decision.id),
                    market_id=decision.market_id,
                    error=str(exc),
                )
                continue
        if result.status == "rejected":
            logger.info(
                "order_rejected",
                decision_id=str(decision.id),
                market_id=decision.market_id,
            )
            continue
        trades.append(
            Trade(
                decision_id=decision.id,
                market_id=order.market_id,
                side=order.side,
                size=order.size,
                price=result.fill_price if result.fill_price is not None else order.price,
                notional_usd=order.notional_usd,
                fees=result.fees,
                status=result.status,
                broker_order_id=result.broker_order_id,
                created_at=now,
                filled_at=now if result.status == "filled" else None,
            )
        )
    return trades


def _persist_agent_notes(
    *,
    factory: Callable[[], AbstractContextManager[Session]],
    predictions: list[Prediction],
) -> None:
    """Persist `inference_log.notes_to_save` entries via manage_notes(write).

    Each entry is a ``{"body": str, "tags": list[str]}`` row produced by
    the trading-agent. The Lead is responsible for the actual write so
    the subagent (which has no DB tool) can still maintain its
    LRU-bounded scratchpad across cycles.
    """
    for prediction in predictions:
        raw_notes = prediction.inference_log.get("notes_to_save")
        if not isinstance(raw_notes, list):
            continue
        for entry in raw_notes:
            if not isinstance(entry, dict):
                continue
            body = entry.get("body")
            if not isinstance(body, str) or not body.strip():
                continue
            tags_raw = entry.get("tags", [])
            tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
            try:
                manage_notes(
                    action="write",
                    agent_id=prediction.agent_id,
                    body=body,
                    tags=tags,
                    session_factory=factory,
                )
            except Exception as exc:
                logger.warning(
                    "note_persist_failed",
                    agent_id=prediction.agent_id,
                    error=str(exc),
                )


def _build_sizing_proposals(
    *,
    predictions: list[Prediction],
    portfolio: PortfolioState,
    adapter: PredictionMarketAdapter | None = None,
) -> list[SizingProposal]:
    """Compute one ``SizingProposal`` per prediction with non-zero notional.

    ``q_market`` is recovered from the prediction itself: since the
    trading-agent computes ``edge = p_yes - q_market`` against the
    side-relevant orderbook quote, we can invert. Predictions with
    out-of-range derived ``q_market`` or zero notional are dropped.

    When ``adapter`` is supplied, ``adapter.estimate_fee`` is called for
    each proposal so risk-execution can solvency-check ``cash >= notional
    + fee``. Tests that don't care about fees may omit it.
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
        side: OrderSide = "yes" if prediction.edge > 0 else "no"
        fee_estimate = _estimate_proposal_fee(
            adapter=adapter,
            market_id=prediction.market_id,
            side=side,
            notional=notional,
            q_market=q_market,
        )
        out.append(
            SizingProposal(
                market_id=prediction.market_id,
                prediction_id=prediction.id,
                proposed_notional_usd=notional,
                side=side,
                q_market=q_market,
                fee_estimate_usd=fee_estimate,
            )
        )
    return out


def _estimate_proposal_fee(
    *,
    adapter: PredictionMarketAdapter | None,
    market_id: str,
    side: OrderSide,
    notional: float,
    q_market: float,
) -> float:
    """Best-effort fee estimate for one proposal.

    Builds a probe ``Order`` with the same ``market_id``/``side``/notional
    the proposal will eventually become and asks the adapter for a fee.
    Returns 0.0 when no adapter is supplied (tests) or when the call
    raises — solvency can still operate, just without the fee buffer.
    """
    if adapter is None:
        return 0.0
    price = q_market if side == "yes" else (1.0 - q_market)
    if price <= 0 or price >= 1:
        return 0.0
    size = notional / price
    probe = Order(
        market_id=market_id,
        side=side,
        size=size,
        price=price,
        notional_usd=notional,
        idempotency_key=f"fee-probe:{market_id}:{side}",
    )
    try:
        return float(adapter.estimate_fee(probe))
    except Exception as exc:
        logger.warning("fee_estimate_failed", market_id=market_id, error=str(exc))
        return 0.0


def _team_cleanup(*, cycle_id: str) -> None:
    """Marker that the Lead has finished the cycle. Asserted by the Stop hook."""
    logger.info("clean up the team", cycle_id=cycle_id)
