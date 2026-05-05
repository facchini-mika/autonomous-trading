"""End-to-end tests for the Lead bootstrap (FakeAdapter, mocked sub-agents)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from execution.lead_bootstrap import CycleAbortedError, bootstrap_team
from execution.subagent_runner import SubagentBudgetError

if TYPE_CHECKING:
    from pytest_mock import MockerFixture
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
        return RiskExecutionOutput(decisions=[decision])

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

    # Predictions arriving from the trading-agent carry no cycle_id; the
    # Lead must stamp it on before the INSERT so outcome_ingestion can
    # later aggregate trade PnL via (cycle_id, market_id).
    prediction_inserts = [
        c for sess in captures for c in sess.execute.call_args_list if "INSERT INTO predictions" in str(c.args[0])
    ]
    assert prediction_inserts
    insert_params = prediction_inserts[0].args[1]
    assert insert_params["cycle_id"] == artifacts.cycle_id
    assert insert_params["cycle_id"]


def test_cycle_plan_jsonb_fields_are_serialised_strings() -> None:
    """All five JSONB columns on cycle_plan must be json.dumps'd, not raw lists.

    Without serialisation, psycopg renders Python lists as Postgres array
    literals, and Postgres then tries to parse each element as a bare JSON
    token — which fails on hex market_ids (`0x7db1...` is not valid JSON).
    """
    captures: list[MagicMock] = []
    bootstrap_team(
        settings=Settings(),
        adapter=FakeAdapter(),
        scanner=_scanner,
        trading=_trading,
        risk=_risk_factory("trade"),
        session_factory=lambda: _capturing_factory(captures),
        clock=_now,
    )
    cycle_plan_calls = [
        c for sess in captures for c in sess.execute.call_args_list if "INSERT INTO cycle_plan" in str(c.args[0])
    ]
    assert len(cycle_plan_calls) == 1
    params = cycle_plan_calls[0].args[1]
    for key in ("next", "holds", "pending", "deferred", "blockers"):
        assert isinstance(params[key], str), (
            f"cycle_plan param {key!r} must be a json-encoded string, got {type(params[key]).__name__}"
        )


def _decision_with_gates(action: str, gate_results: dict[str, Any]) -> Decision:
    return Decision(
        cycle_id="cycle-test",
        market_id="0xa",
        p_consensus=0.7,
        q_market=0.5,
        edge=0.2,
        gate_results=gate_results,
        action=action,
        rationale="test",
        created_at=_now(),
    )


def test_decision_notional_reads_top_level_clipped(mocker: MockerFixture) -> None:
    from execution.lead_bootstrap import _decision_notional

    warnings: list[tuple[str, dict[str, object]]] = []
    mocker.patch(
        "execution.lead_bootstrap.logger.warning",
        side_effect=lambda event, **kw: warnings.append((event, kw)),
    )
    decision = _decision_with_gates("trade", {"clipped_notional": 100.0, "notional_usd": 100.0})
    assert _decision_notional(decision) == 100.0
    assert not any(ev == "decision_notional_missing" for ev, _ in warnings)


def test_decision_notional_logs_warning_when_top_level_keys_missing(
    mocker: MockerFixture,
) -> None:
    """Cycle-4 regression: agent emitted per-gate trail without top-level summary."""
    from execution.lead_bootstrap import _decision_notional

    warnings: list[tuple[str, dict[str, object]]] = []
    mocker.patch(
        "execution.lead_bootstrap.logger.warning",
        side_effect=lambda event, **kw: warnings.append((event, kw)),
    )
    decision = _decision_with_gates(
        "trade",
        {
            "edge_gate": {"passed": True, "reason": "..."},
            "kill_switch": {"passed": True, "reason": "..."},
        },
    )
    assert _decision_notional(decision) == 0.0
    fired = [kw for ev, kw in warnings if ev == "decision_notional_missing"]
    assert len(fired) == 1
    assert fired[0]["gate_results_keys"] == ["edge_gate", "kill_switch"]
    assert fired[0]["action"] == "trade"


def test_decision_notional_silent_for_skip_actions(mocker: MockerFixture) -> None:
    """skip / hold decisions never need top-level notional — no warning expected."""
    from execution.lead_bootstrap import _decision_notional

    warnings: list[tuple[str, dict[str, object]]] = []
    mocker.patch(
        "execution.lead_bootstrap.logger.warning",
        side_effect=lambda event, **kw: warnings.append((event, kw)),
    )
    skip_decision = _decision_with_gates("skip", {"edge_gate": {"passed": False, "reason": "low_edge"}})
    hold_decision = _decision_with_gates("hold", {})
    assert _decision_notional(skip_decision) == 0.0
    assert _decision_notional(hold_decision) == 0.0
    assert not any(ev == "decision_notional_missing" for ev, _ in warnings)


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


def test_bootstrap_skips_scanner_callable_when_limit_eq_topk() -> None:
    """Default settings (LIMIT=TOP_K=50) → Lead never invokes the scanner LLM."""
    scanner_calls: list[Any] = []

    def _scanner_must_not_run(task: Any) -> ScannerReviewerOutput:
        scanner_calls.append(task)
        return _scanner(task)

    captures: list[MagicMock] = []
    artifacts = bootstrap_team(
        settings=Settings(),  # default: UNIVERSE_FETCH_LIMIT=50, TOP_K_MARKETS=50
        adapter=FakeAdapter(),
        scanner=_scanner_must_not_run,
        trading=_trading,
        risk=_risk_factory("skip"),
        session_factory=lambda: _capturing_factory(captures),
        clock=_now,
    )
    assert scanner_calls == [], "scanner LLM callable invoked despite LIMIT<=TOP_K bypass"
    # Bypass should still produce a valid Universe + PortfolioState,
    # and write a subagent_runs audit row with cost_usd=0.
    assert artifacts.cycle_id.startswith("cycle-")
    sql_calls = [
        (str(c.args[0]), c.args[1] if len(c.args) > 1 else None)
        for sess in captures
        for c in sess.execute.call_args_list
    ]
    bypass_audit = [params for sql, params in sql_calls if "INSERT INTO subagent_runs" in sql]
    assert len(bypass_audit) == 1, f"expected one scanner-reviewer bypass audit row, got {len(bypass_audit)}"
    audit_params = bypass_audit[0]
    assert audit_params is not None
    assert audit_params["agent_name"] == "scanner-reviewer"
    assert audit_params["cost_usd"] == 0.0
    assert audit_params["prompt_sha"] == "python-bypass"


def test_bootstrap_places_order_when_risk_emits_alias_keys() -> None:
    """Cycle-7 regression: risk-LLM wrote `final_notional_usd` instead of canonical
    keys → Lead's `_decision_notional` returned 0 → no order. The Pydantic
    alias-normaliser hoists the value at parse-time so the Lead reader is
    unchanged and the order goes through."""

    def _risk_with_aliases(task: Any) -> RiskExecutionOutput:
        prediction = task.predictions[0]
        decision = Decision(
            cycle_id="cycle-test",
            market_id=prediction.market_id,
            p_consensus=prediction.p_yes,
            q_market=0.5,
            edge=prediction.edge,
            gate_results={"final_notional_usd": 168.47, "side": "yes"},
            action="trade",
            rationale="approved (cycle-7 alias pattern)",
            created_at=_now(),
        )
        return RiskExecutionOutput(decisions=[decision])

    fake = FakeAdapter()
    bootstrap_team(
        settings=Settings(),
        adapter=fake,
        scanner=_scanner,
        trading=_trading,
        risk=_risk_with_aliases,
        session_factory=lambda: _capturing_factory([]),
        clock=_now,
    )
    assert len(fake.placed_orders) == 1
    assert fake.placed_orders[0].market_id == "0xa"
    assert fake.placed_orders[0].notional_usd == 168.47


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

    # Force the LLM scanner path by widening the fetch limit beyond top-K;
    # at default settings (LIMIT==TOP_K==50) Lead routes through
    # ``_python_scanner`` and the LLM callable is never invoked.
    settings = Settings(UNIVERSE_FETCH_LIMIT=100)
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

    # Budget abort is an LLM-path-only signal — force the LLM scanner path.
    with pytest.raises(CycleAbortedError) as excinfo:
        bootstrap_team(
            settings=Settings(UNIVERSE_FETCH_LIMIT=100),
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
