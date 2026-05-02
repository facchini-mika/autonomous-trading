"""End-to-end tests for the TaskCreated/TaskCompleted hook scripts.

The hooks run via shebang in production but tests invoke them with
`sys.executable` so the venv Python (which has pydantic installed) is
guaranteed regardless of the caller's PATH.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TASK_CREATED = REPO_ROOT / ".claude" / "hooks" / "task_created_validate.py"
TASK_COMPLETED = REPO_ROOT / ".claude" / "hooks" / "task_completed_validate.py"


def _run(hook: Path, envelope: Mapping[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(envelope),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _portfolio_state_payload() -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "cash": {
            "total_usd": 1000.0,
            "available": 900.0,
            "reserved_for_orders": 100.0,
            "timestamp": now,
        },
        "positions": [],
        "gross_exposure_usd": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "equity": 1000.0,
        "timestamp": now,
    }


def test_unknown_subagent_is_noop_created() -> None:
    result = _run(TASK_CREATED, {"subagent_type": "unknown-role", "payload": {}})
    assert result.returncode == 0
    assert result.stderr == ""


def test_invalid_json_is_noop_created() -> None:
    proc = subprocess.run(
        [sys.executable, str(TASK_CREATED)],
        input="not json",
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0


def test_missing_subagent_type_is_noop_created() -> None:
    result = _run(TASK_CREATED, {"payload": {"foo": "bar"}})
    assert result.returncode == 0


def test_valid_scanner_reviewer_task_passes() -> None:
    envelope = {
        "subagent_type": "scanner-reviewer",
        "payload": {"top_k": 50, "cycle_clock": "2026-05-02T12:00:00Z"},
    }
    result = _run(TASK_CREATED, envelope)
    assert result.returncode == 0


def test_invalid_scanner_reviewer_task_fails() -> None:
    envelope = {
        "subagent_type": "scanner-reviewer",
        "payload": {"top_k": "not-an-int"},
    }
    result = _run(TASK_CREATED, envelope)
    assert result.returncode == 2
    assert "scanner-reviewer" in result.stderr


def test_valid_risk_execution_task_passes() -> None:
    envelope = {
        "subagent_type": "risk-execution",
        "payload": {
            "predictions": [],
            "portfolio_state": _portfolio_state_payload(),
        },
    }
    result = _run(TASK_CREATED, envelope)
    assert result.returncode == 0


def test_completed_unknown_subagent_is_noop() -> None:
    result = _run(TASK_COMPLETED, {"subagent_type": "unknown-role", "payload": {}})
    assert result.returncode == 0


def test_completed_valid_trading_agent_output_passes() -> None:
    envelope = {
        "subagent_type": "trading-agent",
        "payload": {"predictions": []},
    }
    result = _run(TASK_COMPLETED, envelope)
    assert result.returncode == 0


def test_completed_invalid_risk_execution_output_fails() -> None:
    envelope = {
        "subagent_type": "risk-execution",
        "payload": {"decisions": "not-a-list", "trades": []},
    }
    result = _run(TASK_COMPLETED, envelope)
    assert result.returncode == 2
    assert "risk-execution" in result.stderr


def test_completed_missing_subagent_type_is_noop() -> None:
    result = _run(TASK_COMPLETED, {"payload": {"foo": "bar"}})
    assert result.returncode == 0
