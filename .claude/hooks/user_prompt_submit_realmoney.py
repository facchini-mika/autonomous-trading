#!/usr/bin/env python3
"""UserPromptSubmit — surface a real-money intent banner.

Banner-only (no hard block). Triggered when the prompt mentions live trading,
real capital, the capital cap constant, or a TRADING_MODE flip. See
`engineering.md §9` and `CLAUDE.md` (no-go list).
"""

from __future__ import annotations

import json
import re
import sys

TRIGGERS = re.compile(
    r"\b(?:live\s+trade|live\s+trading|echtes\s+kapital|real\s+money|real_capital"
    r"|MAX_CAPITAL_EUR|TRADING_MODE)\b",
    re.IGNORECASE,
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not isinstance(prompt, str) or not prompt:
        return 0

    if not TRIGGERS.search(prompt):
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "REAL-MONEY-INTENT detected. Verify current TRADING_MODE before "
                    "any action. Any change to risk/, MAX_CAPITAL_EUR, or TRADING_MODE "
                    "requires an AUDIT_LOG.md entry (CLAUDE.md, engineering.md §3, §4)."
                ),
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
