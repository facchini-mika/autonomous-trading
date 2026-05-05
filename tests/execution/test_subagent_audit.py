"""Audit-trail tests for ``execution.subagent_runner``.

Re-enable the audit persist (the package conftest disables it by default for
unit tests) and verify the runner emits a single ``subagent_runs`` row on
both success and failure paths, with the right fields populated. DB-level
failures must not poison the validated payload.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel

from execution import subagent_runner
from execution.subagent_runner import (
    SubagentBudgetError,
    SubagentError,
    run_subagent,
)

# Capture the real persist helper at import time, BEFORE the package conftest's
# autouse fixture replaces it with a no-op for every test. Tests that want to
# exercise the real best-effort code path restore from this reference.
_REAL_PERSIST = subagent_runner._persist_subagent_run

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


class _Task(BaseModel):
    market_id: str


class _Output(BaseModel):
    p_yes: float
    reasoning: str


@pytest.fixture
def agent_md(tmp_path: Path) -> Path:
    path = tmp_path / "agent.md"
    path.write_text(
        "---\nname: x\ntools: []\n---\n\n# Role\n\nDo the thing.",
        encoding="utf-8",
    )
    return path


def _envelope(result_text: str, **extra: Any) -> str:
    return json.dumps({"result": result_text, "session_id": "abc", "is_error": False, **extra})


def _mock_run(mocker: MockerFixture, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
    mocker.patch("shutil.which", return_value="/usr/local/bin/claude")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["claude"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )


@pytest.fixture
def captured_persists(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Re-enable the audit persist by routing it into a list, undoing the package-level autouse."""
    captures: list[dict[str, Any]] = []

    def _capture(**kw: Any) -> None:
        captures.append(kw)

    monkeypatch.setattr(subagent_runner, "_persist_subagent_run", _capture)
    return captures


def test_audit_row_written_on_success(
    agent_md: Path,
    mocker: MockerFixture,
    captured_persists: list[dict[str, Any]],
) -> None:
    _mock_run(
        mocker,
        stdout=_envelope(
            '{"p_yes": 0.7, "reasoning": "ok"}',
            total_cost_usd=0.123,
            usage={"input_tokens": 10, "output_tokens": 5},
        ),
    )
    run_subagent(
        agent_md_path=agent_md,
        task=_Task(market_id="0xa"),
        output_model=_Output,
        timeout_s=30,
        cycle_id="cycle-success",
        correlation_id="corr-1",
    )
    assert len(captured_persists) == 1
    row = captured_persists[0]
    assert row["cycle_id"] == "cycle-success"
    assert row["agent_name"] == "agent"
    assert row["correlation_id"] == "corr-1"
    assert row["error"] is None
    assert row["error_class"] is None
    assert row["cost_usd"] == 0.123
    assert row["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert row["envelope"] is not None
    assert row["raw_stdout"] is not None
    assert len(row["prompt_sha"]) == 64
    assert isinstance(row["task_payload"], str)
    assert "0xa" in row["task_payload"]


def test_audit_row_written_on_validation_error(
    agent_md: Path,
    mocker: MockerFixture,
    captured_persists: list[dict[str, Any]],
) -> None:
    _mock_run(mocker, stdout=_envelope('{"p_yes": "not a number", "reasoning": "x"}'))
    with pytest.raises(SubagentError, match="failed validation"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
            cycle_id="cycle-bad",
        )
    assert len(captured_persists) == 1
    row = captured_persists[0]
    assert row["error_class"] == "SubagentError"
    assert "failed validation" in (row["error"] or "")
    assert row["envelope"] is not None  # parsed before validation failed.
    assert row["raw_stdout"] is not None


def test_audit_row_written_on_timeout(
    agent_md: Path,
    mocker: MockerFixture,
    captured_persists: list[dict[str, Any]],
) -> None:
    mocker.patch("shutil.which", return_value="/usr/local/bin/claude")
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
    )
    with pytest.raises(SubagentError, match="timed out"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
            cycle_id="cycle-timeout",
        )
    assert len(captured_persists) == 1
    row = captured_persists[0]
    assert row["error_class"] == "SubagentError"
    assert "timed out" in (row["error"] or "")
    assert row["raw_stdout"] is None
    assert row["envelope"] is None


def test_audit_row_written_on_budget_error(
    agent_md: Path,
    mocker: MockerFixture,
    captured_persists: list[dict[str, Any]],
) -> None:
    _mock_run(mocker, returncode=1, stderr="402 Payment Required: credit balance too low")
    with pytest.raises(SubagentBudgetError):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
            cycle_id="cycle-budget",
        )
    assert len(captured_persists) == 1
    assert captured_persists[0]["error_class"] == "SubagentBudgetError"


def test_db_failure_does_not_poison_validated_payload(
    agent_md: Path,
    mocker: MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the audit DB blows up, ``run_subagent`` still returns the parsed output.

    Patch ``get_session`` (the production seam — the audit helper's try/except
    wraps everything that touches it) and assert the runner returns the
    validated payload unchanged. The package conftest's autouse fixture
    swaps the helper for a no-op; here we restore the real helper so the
    in-helper try/except is the code under test.
    """
    monkeypatch.setattr(subagent_runner, "_persist_subagent_run", _REAL_PERSIST)

    def _explode_session(*_args: Any, **_kw: Any) -> Any:
        msg = "simulated DB outage"
        raise RuntimeError(msg)

    monkeypatch.setattr("execution.subagent_runner.get_session", _explode_session)
    _mock_run(mocker, stdout=_envelope('{"p_yes": 0.42, "reasoning": "still ok"}'))
    result = run_subagent(
        agent_md_path=agent_md,
        task=_Task(market_id="x"),
        output_model=_Output,
        timeout_s=30,
        cycle_id="cycle-db-down",
    )
    assert result.p_yes == 0.42


def test_audit_row_includes_finished_at_after_success(
    agent_md: Path,
    mocker: MockerFixture,
    captured_persists: list[dict[str, Any]],
) -> None:
    _mock_run(mocker, stdout=_envelope('{"p_yes": 0.5, "reasoning": "x"}'))
    run_subagent(
        agent_md_path=agent_md,
        task=_Task(market_id="x"),
        output_model=_Output,
        timeout_s=30,
        cycle_id="cycle-finished",
    )
    row = captured_persists[0]
    assert row["finished_at"] is not None
    assert row["started_at"] is not None
    assert row["finished_at"] >= row["started_at"]
    assert row["latency_ms"] is not None
    assert row["latency_ms"] >= 0


def test_subprocess_env_includes_audit_keys(
    agent_md: Path,
    mocker: MockerFixture,
) -> None:
    """``run_subagent`` must propagate audit identifiers into the subprocess env."""
    captured_env: dict[str, str] = {}

    def _capture_env(_cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_env.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=_envelope('{"p_yes": 0.5, "reasoning": "x"}'),
            stderr="",
        )

    mocker.patch("shutil.which", return_value="/usr/local/bin/claude")
    mocker.patch("subprocess.run", side_effect=_capture_env)
    run_subagent(
        agent_md_path=agent_md,
        task=_Task(market_id="x"),
        output_model=_Output,
        timeout_s=30,
        cycle_id="cycle-env",
        correlation_id="corr-env",
    )
    assert captured_env["CYCLE_ID"] == "cycle-env"
    assert captured_env["AGENT_NAME"] == "agent"
    assert captured_env["CORRELATION_ID"] == "corr-env"
    assert captured_env["RUN_ID"]  # uuid string, non-empty.
