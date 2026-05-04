"""End-to-end tests for the Lead bootstrap (FakeAdapter, mocked sub-agents)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from execution.lead_bootstrap import CycleAbortedError, bootstrap_team
from execution.subagent_runner import SubagentBudgetError
from shared.config.settings import Settings
from shared.models import (
    CashBalance,
    Decision,
    Market,
    Orderbook,
    OrderResult,
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


def test_bootstrap_injects_scanner_thresholds_from_settings() -> None:
    """Lead must hand the scanner-reviewer the threshold knobs from Settings."""
    captured: list[Any] = []

    def _capturing_scanner(task: Any) -> ScannerReviewerOutput:
        captured.append(task)
        return _scanner(task)

    settings = Settings()
    bootstrap_team(
        settings=settings,
        adapter=FakeAdapter(),
        scanner=_capturing_scanner,
        trading=_trading,
        risk=_risk_factory("trade"),
        session_factory=lambda: _capturing_factory([]),
        clock=_now,
    )

    assert len(captured) == 1
    thresholds = captured[0].thresholds
    assert thresholds.min_depth_1pct_usd == settings.MIN_DEPTH_1PCT_USD
    assert thresholds.max_spread == settings.MAX_SPREAD
    assert thresholds.min_ttr_hours == settings.MIN_TIME_TO_RESOLUTION_HOURS
    assert thresholds.max_ttr_days == settings.MAX_TIME_TO_RESOLUTION_DAYS
    assert thresholds.soon_resolve_threshold_days == settings.SOON_RESOLVE_THRESHOLD_DAYS
    assert thresholds.soon_resolve_boost_multiplier == settings.SOON_RESOLVE_BOOST_MULTIPLIER


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

        def estimate_fee(self, order: Any) -> float:
            return 0.0

        def place_order(self, order: Any) -> Any:
            placed.append(order)
            return OrderResult(
                status="filled",
                fill_price=order.price,
                filled_size=order.size,
                fees=0.0,
                broker_order_id="paper-test",
            )

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


def _budget_raiser(_task: Any) -> Any:
    msg = "subagent envelope reports api_error_status='insufficient_quota'"
    raise SubagentBudgetError(msg)


def test_bootstrap_aborts_when_scanner_runs_out_of_credits() -> None:
    captures: list[MagicMock] = []
    fake = FakeAdapter()

    with pytest.raises(CycleAbortedError) as excinfo:
        bootstrap_team(
            settings=Settings(),
            adapter=fake,
            scanner=_budget_raiser,
            trading=_trading,
            risk=_risk_factory("trade"),
            session_factory=lambda: _capturing_factory(captures),
            clock=_now,
        )
    assert excinfo.value.stage == "scanner-reviewer"
    # No orders placed, no predictions/decisions written.
    assert fake.placed_orders == []
    sql = [str(c.args[0]) for sess in captures for c in sess.execute.call_args_list]
    assert not any("INSERT INTO predictions" in s for s in sql)
    assert not any("INSERT INTO decisions" in s for s in sql)


def test_bootstrap_aborts_when_trading_runs_out_of_credits() -> None:
    captures: list[MagicMock] = []
    fake = FakeAdapter()

    with pytest.raises(CycleAbortedError) as excinfo:
        bootstrap_team(
            settings=Settings(),
            adapter=fake,
            scanner=_scanner,
            trading=_budget_raiser,
            risk=_risk_factory("trade"),
            session_factory=lambda: _capturing_factory(captures),
            clock=_now,
        )
    assert excinfo.value.stage == "trading-agent"
    assert fake.placed_orders == []
    sql = [str(c.args[0]) for sess in captures for c in sess.execute.call_args_list]
    assert not any("INSERT INTO predictions" in s for s in sql)
    assert not any("INSERT INTO decisions" in s for s in sql)


def test_bootstrap_aborts_when_risk_runs_out_of_credits() -> None:
    captures: list[MagicMock] = []
    fake = FakeAdapter()

    with pytest.raises(CycleAbortedError) as excinfo:
        bootstrap_team(
            settings=Settings(),
            adapter=fake,
            scanner=_scanner,
            trading=_trading,
            risk=_budget_raiser,
            session_factory=lambda: _capturing_factory(captures),
            clock=_now,
        )
    assert excinfo.value.stage == "risk-execution"
    # Trading-agent already produced predictions, but no orders should land
    # and decisions are not persisted because risk never returned.
    assert fake.placed_orders == []
    sql = [str(c.args[0]) for sess in captures for c in sess.execute.call_args_list]
    assert not any("INSERT INTO decisions" in s for s in sql)
