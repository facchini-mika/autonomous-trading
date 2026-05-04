"""Tests for execution.run_cycle — wiring of subagent callables + adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from execution import run_cycle as run_cycle_mod
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

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def _now() -> datetime:
    return datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_subagent_outputs(mocker: MockerFixture) -> dict[str, Any]:
    """Stub run_subagent so each call returns a canned Pydantic output."""
    market = Market(
        market_id="0xa",
        condition_id="0xa",
        slug="m",
        title="M",
        category="x",
        end_date=_now(),
        status="open",
        created_at=_now(),
        last_seen=_now(),
    )
    orderbook = Orderbook(
        market_id="0xa",
        best_bid=0.4,
        best_ask=0.5,
        mid=0.45,
        depth_bid_1pct=200,
        depth_ask_1pct=200,
        timestamp=_now(),
    )
    portfolio = PortfolioState(
        cash=CashBalance(total_usd=10000.0, available=10000.0, reserved_for_orders=0.0, timestamp=_now()),
        positions=[],
        gross_exposure_usd=0.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        equity=10000.0,
        timestamp=_now(),
    )
    prediction = Prediction(
        market_id="0xa",
        agent_id="trading-agent",
        p_yes=0.7,
        reasoning="catalyst",
        edge=0.20,
        latency_ms=50,
        created_at=_now(),
    )
    decision = Decision(
        cycle_id="cycle-test",
        market_id="0xa",
        p_consensus=0.7,
        q_market=0.5,
        edge=0.2,
        gate_results={"clipped_notional": 50.0},
        action="skip",
        rationale="no",
        created_at=_now(),
    )

    outputs = {
        "scanner": ScannerReviewerOutput(
            universe=Universe(markets=[market], orderbooks={"0xa": orderbook}, timestamp=_now()),
            portfolio_state=portfolio,
        ),
        "trading": TradingAgentOutput(predictions=[prediction]),
        "risk": RiskExecutionOutput(decisions=[decision]),
    }

    counter = {"n": 0}
    sequence = [outputs["scanner"], outputs["trading"], outputs["risk"]]

    def _stub(*, output_model: type, **_: object) -> Any:
        result = sequence[counter["n"]]
        counter["n"] += 1
        return result

    mocker.patch("execution.run_cycle.run_subagent", side_effect=_stub)
    return outputs


def test_main_wires_factory_subagents_and_lead(
    fake_subagent_outputs: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    fake_adapter = MagicMock()
    mocker.patch("execution.run_cycle.make_adapter", return_value=fake_adapter)
    mock_bootstrap = mocker.patch("execution.run_cycle.bootstrap_team")
    mock_bootstrap.return_value = MagicMock(
        cycle_id="cycle-1",
        predictions=[],
        decisions=[],
        trades=[],
    )

    result = run_cycle_mod.main()

    assert result == 0
    mock_bootstrap.assert_called_once()
    kwargs = mock_bootstrap.call_args.kwargs
    assert kwargs["adapter"] is fake_adapter
    assert isinstance(kwargs["settings"], Settings)
    # The three subagent callables are present and callable.
    for name in ("scanner", "trading", "risk"):
        assert callable(kwargs[name])


def test_subagent_callables_dispatch_to_runner(
    fake_subagent_outputs: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    mocker.patch("execution.run_cycle.make_adapter", return_value=MagicMock())
    captured_callables: dict[str, Any] = {}

    def _capture(*, scanner: Any, trading: Any, risk: Any, **_: object) -> Any:
        captured_callables["scanner"] = scanner
        captured_callables["trading"] = trading
        captured_callables["risk"] = risk
        return MagicMock(cycle_id="x", predictions=[], decisions=[], trades=[])

    mocker.patch("execution.run_cycle.bootstrap_team", side_effect=_capture)
    run_cycle_mod.main()

    # Each callable should invoke run_subagent under the hood.
    captured_callables["scanner"](MagicMock())
    captured_callables["trading"](MagicMock())
    captured_callables["risk"](MagicMock())
    assert run_cycle_mod.run_subagent.call_count >= 3  # type: ignore[attr-defined]


def test_logs_correlation_id(
    fake_subagent_outputs: dict[str, Any],
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mocker.patch("execution.run_cycle.make_adapter", return_value=MagicMock())
    mocker.patch(
        "execution.run_cycle.bootstrap_team",
        return_value=MagicMock(
            cycle_id="cycle-1",
            predictions=[],
            decisions=[],
            trades=[],
        ),
    )
    run_cycle_mod.main()
    captured = capsys.readouterr()
    assert "run_cycle_start" in captured.out
    assert "run_cycle_done" in captured.out
    assert "correlation_id" in captured.out
