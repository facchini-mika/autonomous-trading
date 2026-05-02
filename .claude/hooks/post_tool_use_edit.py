#!/usr/bin/env python3
"""PostToolUse:Edit|Write — run ruff on the edited file.

Phase 2: only ruff check is enforced. Phase 3 will add `mypy --strict` and a
targeted `pytest` subset (see `engineering.md §9`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

PYTHON_SUFFIXES = {".py", ".pyi"}


def _file_path(payload: dict[str, object]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    return fp if isinstance(fp, str) else ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    file_path = _file_path(payload)
    if not file_path:
        return 0
    if Path(file_path).suffix not in PYTHON_SUFFIXES:
        return 0
    if not Path(file_path).exists():
        return 0

    uv = shutil.which("uv")
    if not uv:
        # uv not available: skip silently rather than fail the workflow.
        return 0

    result = subprocess.run(
        [uv, "run", "ruff", "check", "--quiet", file_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.stderr.write(f"\nruff check failed for {file_path}\n")
        return 2

    # Phase 3: enable mypy --strict and pytest -k <heuristic> here.
    return 0


if __name__ == "__main__":
    sys.exit(main())
