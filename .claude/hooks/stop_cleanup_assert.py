#!/usr/bin/env python3
"""Stop — assert team cleanup ran before lead exits.

No-op outside team-lead sessions. In a team-lead session, requires the marker
file `~/.claude/teams/<team-name>/.cleanup_done` written by the built-in
`Clean up the team` command. Missing marker => exit 2 with stderr warning so
the cycle is flagged. See `specs/engineering.md §9` and `specs/orchestration.md`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    team_name = os.environ.get("CLAUDE_TEAM_NAME", "").strip()
    if not team_name:
        return 0

    marker = Path.home() / ".claude" / "teams" / team_name / ".cleanup_done"
    if marker.exists():
        return 0

    sys.stderr.write(
        f"stop_cleanup_assert: missing cleanup marker {marker}. Lead must call 'Clean up the team' before exit.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
