#!/usr/bin/env python3
"""TaskCompleted — validate output artifact against shared.models.

Phase 2 stub: shared.models does not exist yet, so the hook degrades to a
no-op via ImportError. Phase 3 activates Pydantic validation per member:
- scanner-reviewer => Universe + PortfolioState
- trading-agent    => Prediction[]
- risk-execution   => Decision[] + Trade[]
On schema mismatch the hook exits 2, which forces a task retry per
`engineering.md §9` and `plan.md` Phase 3, step 18.
"""

from __future__ import annotations

import sys

try:
    # Phase 3: replace with concrete model imports + validation logic.
    from shared.models import Decision, Prediction, Trade  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
    sys.exit(0)

# Phase 3: read stdin JSON, dispatch by member role, validate output artifact,
# exit 2 on Pydantic ValidationError so the cycle retries the task.
sys.exit(0)
