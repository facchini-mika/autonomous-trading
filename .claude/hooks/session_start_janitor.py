#!/usr/bin/env python3
"""SessionStart — sweep stale team dirs and validate the team spec.

No-op in dev sessions: when no team marker is present (no `CLAUDE_TEAM_NAME`
env and no `~/.claude/teams/` directory), the hook exits 0 immediately.

In team-cycle sessions:
- sweep stale `~/.claude/teams/<name>/` directories older than 1h,
- parse `.claude/teams/trading-team.spec.json` and validate required fields,
- exit 1 on a broken spec so the cycle aborts and cron retries.

See `engineering.md §9` and `orchestration.md §5`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

STALE_SECONDS = 60 * 60  # 1 hour
REQUIRED_TOP_LEVEL = ("name", "lead", "members", "cycle_lifetime_minutes")
REQUIRED_MEMBER = ("agent", "role")


def _is_dev_session() -> bool:
    if os.environ.get("CLAUDE_TEAM_NAME"):
        return False
    teams_root = Path.home() / ".claude" / "teams"
    return not (teams_root.exists() and any(teams_root.iterdir()))


def _sweep(teams_root: Path) -> None:
    if not teams_root.exists():
        return
    cutoff = time.time() - STALE_SECONDS
    for child in teams_root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _validate_spec(spec_path: Path) -> int:
    if not spec_path.exists():
        sys.stderr.write(f"team spec not found: {spec_path}\n")
        return 1
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"team spec unreadable: {exc}\n")
        return 1

    for field in REQUIRED_TOP_LEVEL:
        if field not in spec:
            sys.stderr.write(f"team spec missing required field: {field}\n")
            return 1

    members = spec.get("members")
    if not isinstance(members, list) or not members:
        sys.stderr.write("team spec 'members' must be a non-empty list\n")
        return 1
    for idx, member in enumerate(members):
        if not isinstance(member, dict):
            sys.stderr.write(f"team spec members[{idx}] must be an object\n")
            return 1
        for field in REQUIRED_MEMBER:
            if field not in member:
                sys.stderr.write(f"team spec members[{idx}] missing field: {field}\n")
                return 1

    return 0


def main() -> int:
    if _is_dev_session():
        return 0

    teams_root = Path.home() / ".claude" / "teams"
    _sweep(teams_root)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    spec_path = (
        Path(project_dir) / ".claude" / "teams" / "trading-team.spec.json"
        if project_dir
        else Path(".claude/teams/trading-team.spec.json")
    )
    return _validate_spec(spec_path)


if __name__ == "__main__":
    sys.exit(main())
