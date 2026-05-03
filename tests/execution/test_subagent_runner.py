"""Tests for execution.subagent_runner — subprocess-mocked Claude invocations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from execution.subagent_runner import SubagentError, run_subagent

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


def _envelope(result_text: str) -> str:
    return json.dumps({"result": result_text, "session_id": "abc", "is_error": False})


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


def test_success_returns_validated_output(agent_md: Path, mocker: MockerFixture) -> None:
    _mock_run(mocker, stdout=_envelope('{"p_yes": 0.7, "reasoning": "context"}'))
    result = run_subagent(
        agent_md_path=agent_md,
        task=_Task(market_id="0xa"),
        output_model=_Output,
        timeout_s=30,
    )
    assert result.p_yes == 0.7
    assert result.reasoning == "context"


def test_strips_markdown_fences(agent_md: Path, mocker: MockerFixture) -> None:
    fenced = '```json\n{"p_yes": 0.5, "reasoning": "x"}\n```'
    _mock_run(mocker, stdout=_envelope(fenced))
    result = run_subagent(
        agent_md_path=agent_md,
        task=_Task(market_id="0xa"),
        output_model=_Output,
        timeout_s=30,
    )
    assert result.p_yes == 0.5


def test_missing_agent_skeleton_raises(tmp_path: Path) -> None:
    with pytest.raises(SubagentError, match="agent skeleton not found"):
        run_subagent(
            agent_md_path=tmp_path / "missing.md",
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
        )


def test_missing_claude_binary_raises(agent_md: Path, mocker: MockerFixture) -> None:
    mocker.patch("shutil.which", return_value=None)
    with pytest.raises(SubagentError, match="not on PATH"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
        )


def test_timeout_raises(agent_md: Path, mocker: MockerFixture) -> None:
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
        )


def test_nonzero_exit_raises(agent_md: Path, mocker: MockerFixture) -> None:
    _mock_run(mocker, returncode=1, stderr="rate limited")
    with pytest.raises(SubagentError, match="exited 1"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
        )


def test_malformed_envelope_raises(agent_md: Path, mocker: MockerFixture) -> None:
    _mock_run(mocker, stdout="not json")
    with pytest.raises(SubagentError, match="non-JSON envelope"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
        )


def test_envelope_missing_result_raises(agent_md: Path, mocker: MockerFixture) -> None:
    _mock_run(mocker, stdout=json.dumps({"session_id": "abc"}))
    with pytest.raises(SubagentError, match="missing 'result'"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
        )


def test_schema_mismatch_raises(agent_md: Path, mocker: MockerFixture) -> None:
    _mock_run(mocker, stdout=_envelope('{"p_yes": "not a number", "reasoning": "x"}'))
    with pytest.raises(SubagentError, match="failed validation"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
        )


def test_mcp_config_appended_when_provided(agent_md: Path, tmp_path: Path, mocker: MockerFixture) -> None:
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=_envelope('{"p_yes": 0.5, "reasoning": "x"}'),
            stderr="",
        )

    mocker.patch("shutil.which", return_value="/usr/local/bin/claude")
    mocker.patch("subprocess.run", side_effect=_fake_run)

    run_subagent(
        agent_md_path=agent_md,
        task=_Task(market_id="x"),
        output_model=_Output,
        timeout_s=30,
        mcp_config_path=mcp,
        allowed_mcp_tools=("mcp__research__web_search",),
    )
    cmd = captured["cmd"]
    assert "--mcp-config" in cmd
    assert str(mcp) in cmd
    assert "--allowed-tools" in cmd
    assert "mcp__research__web_search" in cmd


def test_mcp_config_missing_raises(agent_md: Path, tmp_path: Path, mocker: MockerFixture) -> None:
    mocker.patch("shutil.which", return_value="/usr/local/bin/claude")
    with pytest.raises(SubagentError, match="mcp config not found"):
        run_subagent(
            agent_md_path=agent_md,
            task=_Task(market_id="x"),
            output_model=_Output,
            timeout_s=30,
            mcp_config_path=tmp_path / "missing.json",
        )


def test_doctrine_concatenated(agent_md: Path, tmp_path: Path, mocker: MockerFixture) -> None:
    doctrine = tmp_path / "doctrine.md"
    doctrine.write_text("# Doctrine\n\nbe careful with edge.", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=_envelope('{"p_yes": 0.5, "reasoning": "x"}'),
            stderr="",
        )

    mocker.patch("shutil.which", return_value="/usr/local/bin/claude")
    mocker.patch("subprocess.run", side_effect=_fake_run)

    run_subagent(
        agent_md_path=agent_md,
        doctrine_path=doctrine,
        task=_Task(market_id="x"),
        output_model=_Output,
        timeout_s=30,
    )
    sys_prompt_idx = captured["cmd"].index("--append-system-prompt") + 1
    sys_prompt = captured["cmd"][sys_prompt_idx]
    assert "Do the thing." in sys_prompt
    assert "be careful with edge." in sys_prompt
    assert "schema" in sys_prompt.lower()
