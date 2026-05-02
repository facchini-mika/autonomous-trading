"""Sanity tests for infra/scripts/run_cycle.sh — argument dispatch only.

We mock `timeout` and `uv` via PATH overrides so the test never actually invokes
the cycle entry points; we just assert the wrapper picks the correct module
for each known cycle name and rejects unknown names.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "scripts" / "run_cycle.sh"


def _make_stub_path(tmp_path: Path) -> Path:
    """Create a directory with stub `timeout` and `uv` binaries that echo args."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "timeout"
    stub.write_text(
        '#!/usr/bin/env bash\necho "timeout-args: $*"\nexit 0\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    uv_stub = bindir / "uv"
    uv_stub.write_text(
        '#!/usr/bin/env bash\necho "uv-args: $*"\nexit 0\n',
        encoding="utf-8",
    )
    uv_stub.chmod(uv_stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bindir


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize(
    ("cycle", "expected_module"),
    [
        ("trading_cycle", "execution.run_cycle"),
        ("outcome_ingestion", "execution.outcome_ingestion"),
        ("lessons_summary", "research.skills.lessons_summary"),
    ],
)
def test_known_cycle_dispatches_to_module(cycle: str, expected_module: str, tmp_path: Path) -> None:
    bindir = _make_stub_path(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(SCRIPT), cycle],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert expected_module in result.stdout


def test_unknown_cycle_exits_with_usage_error(tmp_path: Path) -> None:
    bindir = _make_stub_path(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = subprocess.run(
        ["bash", str(SCRIPT), "unknown_cycle"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "unknown cycle" in result.stderr


def test_missing_arg_exits_nonzero() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
