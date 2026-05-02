"""End-to-end tests for the Lead bootstrap (FakeAdapter, mocked sub-agents)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from execution.lead_bootstrap import bootstrap_team
from shared.config.settings import Settings
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


def _now() -> datetime:
    return datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


def _market(market_id: str = "0xa") -> Market:
    return Market(
        market_id=market_id,
        condition_id=market_id,
        slug="m",
        title="M",
        category="x",
        end_date=_now(),
        status="open",
        created_at=_now(),
        last_seen=_now(),
    )


def _orderbook(market_id: str = "0xa") -> Orderbook:
    return Orderbook(
        market_id=market_id,
        best_bid=0.4,
        best_ask=0.5,
        mid=0.45,
        depth_bid_1pct=200,
        depth_ask_1pct=200,
        timestamp=_now(),
    )


def _portfolio() -> PortfolioState:
    return PortfolioState(
        cash=CashBalance(total_usd=10000.0, available=10000.0, reserved_for_orders=0.0, timestamp=_now()),
        positions=[],
        gross_exposure_usd=0.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        equity=10000.0,
        timestamp=_now(),
    )


def _scanner(_task: Any) -> ScannerReviewerOutput:
    return ScannerReviewerOutput(
        universe=Universe(markets=[_market()], orderbooks={"0xa": _orderbook()}, timestamp=_now()),
        portfolio_state=_portfolio(),
    )


def _trading(_task: Any) -> TradingAgentOutput:
    return TradingAgentOutput(
        predictions=[
            Prediction(
                market_id="0xa",
                agent_id="trading-agent",
                p_yes=0.7,
                reasoning="catalyst observed",
                edge=0.20,
                latency_ms=50,
                created_at=_now(),
            ),
        ],
    )


def _risk_factory(decision_action: str = "trade") -> Any:
    def _risk(task: Any) -> RiskExecutionOutput:
        prediction = task.predictions[0]
        decision = Decision(
            cycle_id="cycle-test",
            market_id=prediction.market_id,
            p_consensus=prediction.p_yes,
            q_market=0.5,
            edge=prediction.edge,
            gate_results={"clipped_notional": 50.0, "notional_usd": 50.0},
            action=decision_action,
            rationale="approved by gates",
            created_at=_now(),
        )
        return RiskExecutionOutput(decisions=[decision], trades=[])

    return _risk


@contextmanager
def _capturing_factory(captures: list[Any]) -> Iterator[MagicMock]:
    sess = MagicMock()
    sess.execute.return_value.first.return_value = None
    captures.append(sess)
    yield sess


def test_bootstrap_writes_predictions_and_decisions() -> None:
    captures: list[MagicMock] = []

    artifacts = bootstrap_team(
        settings=Settings(),
        adapter=FakeAdapter(),
        scanner=_scanner,
        trading=_trading,
        risk=_risk_factory("trade"),
        session_factory=lambda: _capturing_factory(captures),
        clock=_now,
    )

    assert len(artifacts.predictions) == 1
    assert len(artifacts.decisions) == 1
    assert len(captures) >= 1
    sql_calls = [str(c.args[0]) for sess in captures for c in sess.execute.call_args_list]
    assert any("INSERT INTO predictions" in s for s in sql_calls)
    assert any("INSERT INTO decisions" in s for s in sql_calls)
    assert any("INSERT INTO cycle_plan" in s for s in sql_calls)


def test_bootstrap_places_order_only_for_trade_decisions() -> None:
    fake = FakeAdapter()
    bootstrap_team(
        settings=Settings(),
        adapter=fake,
        scanner=_scanner,
        trading=_trading,
        risk=_risk_factory("trade"),
        session_factory=lambda: _capturing_factory([]),
        clock=_now,
    )
    assert len(fake.placed_orders) == 1
    assert fake.placed_orders[0].market_id == "0xa"


def test_bootstrap_skips_order_for_skip_decision() -> None:
    fake = FakeAdapter()
    bootstrap_team(
        settings=Settings(),
        adapter=fake,
        scanner=_scanner,
        trading=_trading,
        risk=_risk_factory("skip"),
        session_factory=lambda: _capturing_factory([]),
        clock=_now,
    )
    assert fake.placed_orders == []


def test_bootstrap_skips_order_for_hold_decision() -> None:
    fake = FakeAdapter()
    bootstrap_team(
        settings=Settings(),
        adapter=fake,
        scanner=_scanner,
        trading=_trading,
        risk=_risk_factory("hold"),
        session_factory=lambda: _capturing_factory([]),
        clock=_now,
    )
    assert fake.placed_orders == []


def test_bootstrap_returns_cycle_plan() -> None:
    artifacts = bootstrap_team(
        settings=Settings(),
        adapter=FakeAdapter(),
        scanner=_scanner,
        trading=_trading,
        risk=_risk_factory("trade"),
        session_factory=lambda: _capturing_factory([]),
        clock=_now,
    )
    assert artifacts.cycle_plan.written_by_cycle_id == artifacts.cycle_id
    assert artifacts.cycle_id.startswith("cycle-")


def test_default_trading_mode_is_paper_e2e() -> None:
    """Cross-stream acceptance test from plan.md."""
    settings = Settings()
    assert settings.TRADING_MODE == "paper"
    placed: list[Any] = []

    class _Adapter:
        def get_markets(self, *, limit: int) -> list[Market]:
            return []

        def get_orderbook(self, market_id: str) -> Orderbook:
            return _orderbook(market_id)

        def get_metadata(self, market_id: str):
            return None

        def get_resolution(self, market_id: str):
            return None

        def place_order(self, order: Any) -> Any:
            placed.append(order)
            return MagicMock()

        def cancel_order(self, order_id: str) -> Any:
            return MagicMock()

    bootstrap_team(
        settings=settings,
        adapter=_Adapter(),
        scanner=_scanner,
        trading=_trading,
        risk=_risk_factory("trade"),
        session_factory=lambda: _capturing_factory([]),
        clock=_now,
    )
    assert len(placed) == 1
