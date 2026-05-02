#!/usr/bin/env python3
"""TaskCreated — validate task payload against shared.models.

Routes the stdin payload by `subagent_type` and validates the matching
input wrapper from `shared.models.tasks`. Unknown subagent types are a
no-op (exit 0). Validation errors exit 2 with stderr so the agent-team
runtime treats the task as malformed and retries (specs/engineering.md §9).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Self-bootstrap: hook scripts run via shebang; the project package isn't
# installed, so make `shared` importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pydantic import BaseModel, ValidationError

from shared.models.tasks import (
    RiskExecutionTask,
    ScannerReviewerTask,
    TradingAgentTask,
)

ROUTING: dict[str, type[BaseModel]] = {
    "scanner-reviewer": ScannerReviewerTask,
    "trading-agent": TradingAgentTask,
    "risk-execution": RiskExecutionTask,
}


def main() -> int:
    try:
        envelope = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    if not isinstance(envelope, dict):
        return 0

    subagent_type = envelope.get("subagent_type")
    if not isinstance(subagent_type, str):
        return 0

    model_cls = ROUTING.get(subagent_type)
    if model_cls is None:
        return 0

    payload = envelope.get("payload", envelope)
    try:
        model_cls.model_validate(payload)
    except ValidationError as exc:
        sys.stderr.write(f"task_created_validate: payload for '{subagent_type}' failed schema check\n{exc}\n")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
