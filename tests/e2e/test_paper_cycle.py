"""End-to-end paper-cycle test: trading_cycle → outcome_ingestion → lessons_summary.

Runs the three Phase-5 entry points sequentially against a real Postgres
service container, with FakeAdapter (CLOB stand-in) and FakeGamma. Asserts
on the rows persisted to predictions, decisions, paper_trades, cycle_plan,
system_state, and lessons.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from execution import outcome_ingestion
from execution.decision_context import require_decision_id
from execution.gamma_client import GammaClient
from execution.lead_bootstrap import bootstrap_team
from research.skills import lessons_summary
from shared.adapters.paper_trading import PaperTradingAdapter
from shared.config.settings import Settings
from shared.db import get_session
from shared.models import (
    CashBalance,
    Decision,
    Market,
    Orderbook,
    PortfolioState,
    Prediction,
    RiskExecutionOutput,
    ScannerReviewerOutput,
    TradingAgentOutput,
    Universe,
)
from tests.adapters.fake_adapter import FakeAdapter
from tests.adapters.fake_gamma import FakeGamma

if TYPE_CHECKING:
    import psycopg

CYCLE_CLOCK = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
MARKET_ID = "0xa11ce"


def _market() -> Market:
    return Market(
        market_id=MARKET_ID,
        condition_id=MARKET_ID,
        slug="will-x-happen",
        title="Will X happen?",
        category="politics",
        end_date=CYCLE_CLOCK + timedelta(days=7),
        status="open",
        created_at=CYCLE_CLOCK,
        last_seen=CYCLE_CLOCK,
    )


def _orderbook() -> Orderbook:
    # Spread = 0.05, well within MAX_SPREAD=0.10 ceiling enforced by
    # _enforce_universe_invariants in lead_bootstrap.
    return Orderbook(
        market_id=MARKET_ID,
        best_bid=0.475,
        best_ask=0.525,
        mid=0.50,
        depth_bid_1pct=500,
        depth_ask_1pct=500,
        timestamp=CYCLE_CLOCK,
    )


def _portfolio() -> PortfolioState:
    return PortfolioState(
        cash=CashBalance(
            total_usd=10000.0,
            available=10000.0,
            reserved_for_orders=0.0,
            timestamp=CYCLE_CLOCK,
        ),
        positions=[],
        gross_exposure_usd=0.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        equity=10000.0,
        timestamp=CYCLE_CLOCK,
    )


def _scanner(_task: Any) -> ScannerReviewerOutput:
    return ScannerReviewerOutput(
        universe=Universe(markets=[_market()], orderbooks={MARKET_ID: _orderbook()}, timestamp=CYCLE_CLOCK),
        portfolio_state=_portfolio(),
    )


def _trading(_task: Any) -> TradingAgentOutput:
    return TradingAgentOutput(
        predictions=[
            Prediction(
                market_id=MARKET_ID,
                agent_id="trading-agent",
                p_yes=0.7,
                reasoning="hypothesis: catalyst observed",
                edge=0.20,
                latency_ms=42,
                created_at=CYCLE_CLOCK,
            ),
        ],
    )


def _risk(task: Any) -> RiskExecutionOutput:
    prediction = task.predictions[0]
    decision = Decision(
        cycle_id="cycle-test",
        market_id=prediction.market_id,
        p_consensus=prediction.p_yes,
        q_market=0.5,
        edge=prediction.edge,
        gate_results={"clipped_notional": 100.0, "notional_usd": 100.0},
        action="trade",
        rationale="approved by gates",
        created_at=CYCLE_CLOCK,
    )
    return RiskExecutionOutput(decisions=[decision])


def _table_count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def test_paper_cycle_full_pipeline(clean_db: None, owner_conn: psycopg.Connection) -> None:
    settings = Settings()
    assert settings.TRADING_MODE == "paper"

    fake_live = FakeAdapter()
    fake_live.markets[MARKET_ID] = _market()
    fake_live.orderbooks[MARKET_ID] = _orderbook()

    paper = PaperTradingAdapter(
        live_adapter=fake_live,
        session_factory=lambda: get_session("trading_cycle"),
        decision_id_provider=require_decision_id,
        clock=lambda: CYCLE_CLOCK,
    )

    artifacts = bootstrap_team(
        settings=settings,
        adapter=paper,
        scanner=_scanner,
        trading=_trading,
        risk=_risk,
        session_factory=lambda: get_session("trading_cycle"),
        clock=lambda: CYCLE_CLOCK,
    )

    assert _table_count(owner_conn, "predictions") == 1
    assert _table_count(owner_conn, "decisions") == 1
    assert _table_count(owner_conn, "cycle_plan") == 1
    assert _table_count(owner_conn, "paper_trades") == 1
    assert artifacts.cycle_id.startswith("cycle-")

    gamma = FakeGamma()
    gamma.add_resolution(market_id=MARKET_ID, outcome=False, resolved_at=CYCLE_CLOCK + timedelta(hours=1))
    counts = outcome_ingestion.run_once(
        gamma=cast("GammaClient", gamma),
        clock=lambda: CYCLE_CLOCK + timedelta(hours=2),
    )
    assert counts["predictions"] == 1
    assert counts["paper_trades"] == 1

    with owner_conn.cursor() as cur:
        cur.execute("SELECT outcome, realized_pnl FROM predictions WHERE market_id = %s", (MARKET_ID,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False
    cur_paper = owner_conn.cursor()
    cur_paper.execute("SELECT realized_pnl FROM paper_trades WHERE market_id = %s", (MARKET_ID,))
    pnl_row = cur_paper.fetchone()
    assert pnl_row is not None
    assert pnl_row[0] is not None

    inserted = lessons_summary.run_once(
        settings=settings,
        clock=lambda: CYCLE_CLOCK + timedelta(hours=3),
    )
    assert inserted == 1
    assert _table_count(owner_conn, "lessons") == 1

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT key FROM system_state WHERE key IN ('last_outcome_ingestion_at', 'last_lessons_summary_at')"
        )
        keys = {r[0] for r in cur.fetchall()}
    assert keys == {"last_outcome_ingestion_at", "last_lessons_summary_at"}


def test_all_eleven_tables_present(owner_conn: psycopg.Connection) -> None:
    expected = {
        "markets",
        "market_snapshots",
        "predictions",
        "decisions",
        "trades",
        "paper_trades",
        "positions",
        "notes",
        "cycle_plan",
        "lessons",
        "system_state",
    }
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (sorted(expected),),
        )
        present = {r[0] for r in cur.fetchall()}
    assert present == expected
