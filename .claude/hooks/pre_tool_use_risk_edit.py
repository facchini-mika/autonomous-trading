#!/usr/bin/env python3
"""PreToolUse:Edit|Write — gate edits under `risk/**` to Plan Mode.

Edits to `risk/**` require Plan Mode with explicit prior user approval (see
`CLAUDE.md` and `specs/engineering.md §9`). Outside Plan Mode the hook returns an
"ask" decision so the user must intercept; this is the safest default given
that Claude Code does not currently expose a Plan-Mode flag in the hook
stdin payload.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROTECTED_PREFIX = "risk/"


def _file_path(payload: dict[str, object]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    return fp if isinstance(fp, str) else ""


def _is_protected(file_path: str) -> bool:
    if not file_path:
        return False
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    try:
        target = Path(file_path).resolve()
    except OSError:
        return False
    if project_dir:
        try:
            rel = target.relative_to(Path(project_dir).resolve())
        except ValueError:
            return False
        return str(rel).startswith(PROTECTED_PREFIX)
    return target.as_posix().split("/risk/", 1)[0] != target.as_posix() and "/risk/" in target.as_posix()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    file_path = _file_path(payload)
    if not _is_protected(file_path):
        return 0

    decision = "ask"
    reason = (
        "Edit under `risk/**` requires Plan Mode with prior user approval "
        "(CLAUDE.md, specs/engineering.md §9). User must explicitly authorize."
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
