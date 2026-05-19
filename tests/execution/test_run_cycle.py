"""Tests for execution.run_cycle — wiring of subagent callables + adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from execution import run_cycle as run_cycle_mod
from shared.config.settings import Settings
from shared.models import (
    Decision,
    Prediction,
    RiskExecutionOutput,
    TradingAgentOutput,
)

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def _now() -> datetime:
    return datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake_subagent_outputs(mocker: MockerFixture) -> dict[str, Any]:
    """Stub run_subagent so each call returns a canned Pydantic output.

    Scanner is now deterministic Python inside the Lead — only trading-agent
    and risk-execution go through ``run_subagent``.
    """
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
        "trading": TradingAgentOutput(predictions=[prediction]),
        "risk": RiskExecutionOutput(decisions=[decision]),
    }

    counter = {"n": 0}
    sequence = [outputs["trading"], outputs["risk"]]

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
    # The two LLM subagent callables are present and callable; the scanner
    # is Lead-internal Python and isn't passed in.
    for name in ("trading", "risk"):
        assert callable(kwargs[name])
    assert "scanner" not in kwargs


def test_subagent_callables_dispatch_to_runner(
    fake_subagent_outputs: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    mocker.patch("execution.run_cycle.make_adapter", return_value=MagicMock())
    captured_callables: dict[str, Any] = {}

    def _capture(*, trading: Any, risk: Any, **_: object) -> Any:
        captured_callables["trading"] = trading
        captured_callables["risk"] = risk
        return MagicMock(cycle_id="x", predictions=[], decisions=[], trades=[])

    mocker.patch("execution.run_cycle.bootstrap_team", side_effect=_capture)
    run_cycle_mod.main()

    # Each callable should invoke run_subagent under the hood.
    captured_callables["trading"](MagicMock())
    captured_callables["risk"](MagicMock())
    assert run_cycle_mod.run_subagent.call_count >= 2  # type: ignore[attr-defined]


def test_closures_forward_cycle_id_and_correlation_id(
    fake_subagent_outputs: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    """Both LLM closures must forward task.cycle_id + the cycle's correlation_id."""
    mocker.patch("execution.run_cycle.make_adapter", return_value=MagicMock())
    captured_callables: dict[str, Any] = {}

    def _capture(*, trading: Any, risk: Any, **_: object) -> Any:
        captured_callables["trading"] = trading
        captured_callables["risk"] = risk
        return MagicMock(cycle_id="x", predictions=[], decisions=[], trades=[])

    mocker.patch("execution.run_cycle.bootstrap_team", side_effect=_capture)
    run_cycle_mod.main()

    for closure_name, cycle_id_value in (
        ("trading", "cycle-trade-id"),
        ("risk", "cycle-risk-id"),
    ):
        task = MagicMock(cycle_id=cycle_id_value)
        captured_callables[closure_name](task)

    calls = run_cycle_mod.run_subagent.call_args_list  # type: ignore[attr-defined]
    cycle_ids_passed = [c.kwargs["cycle_id"] for c in calls]
    assert "cycle-trade-id" in cycle_ids_passed
    assert "cycle-risk-id" in cycle_ids_passed
    # correlation_id is the same across both (minted once per main()).
    correlation_ids = {c.kwargs.get("correlation_id") for c in calls}
    assert len(correlation_ids - {None}) == 1
    assert next(iter(correlation_ids - {None}))  # non-empty hex.


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
