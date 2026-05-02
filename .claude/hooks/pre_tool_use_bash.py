#!/usr/bin/env python3
"""PreToolUse:Bash — block destructive commands.

Blocks `rm -rf`, `git push --force`, `git reset --hard`, writes to `.env*`,
and `--no-verify`. Hardcoded blocklist; always active. See `specs/engineering.md §9`.
"""

from __future__ import annotations

import json
import re
import sys

BLOCKLIST: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"), "rm -rf is forbidden"),
    (re.compile(r"\bgit\s+push\s+(?:[^|;&]*\s)?--force\b"), "git push --force is forbidden"),
    (re.compile(r"\bgit\s+push\s+(?:[^|;&]*\s)?-f\b"), "git push -f is forbidden"),
    (re.compile(r"\bgit\s+reset\s+(?:[^|;&]*\s)?--hard\b"), "git reset --hard is forbidden"),
    (re.compile(r"(?:>|>>|tee\s+(?:-a\s+)?)\s*\.env(?:\.[A-Za-z0-9_.-]+)?\b"), "writes to .env* are forbidden"),
    (re.compile(r"--no-verify\b"), "--no-verify is forbidden"),
    (re.compile(r"--no-gpg-sign\b"), "--no-gpg-sign is forbidden"),
]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command:
        return 0

    for pattern, reason in BLOCKLIST:
        if pattern.search(command):
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"Blocked by pre_tool_use_bash: {reason}",
                    }
                },
                sys.stdout,
            )
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
