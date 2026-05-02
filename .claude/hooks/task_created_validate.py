#!/usr/bin/env python3
"""TaskCreated — validate task payload against shared.models.

Phase 2 stub: shared.models does not exist yet, so the hook degrades to a
no-op via ImportError. Phase 3 activates Pydantic validation against
`shared.models` (Universe, PortfolioState, Prediction, Decision, Trade) per
`engineering.md §9` and `plan.md` Phase 3, step 18.
"""

from __future__ import annotations

import sys

try:
    # Phase 3: replace with concrete model imports + validation logic.
    from shared.models import Decision, Prediction, Trade  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
    sys.exit(0)

# Phase 3: read stdin JSON, dispatch by member role, validate input artifact,
# exit 2 on Pydantic ValidationError so the cycle retries the task.
sys.exit(0)
