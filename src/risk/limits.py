"""Risk-gate constants — frozen literals, no Settings imports.

`risk/` is import-isolated (see import-linter risk-isolation contract): it
must not pull `shared.config` or `shared.db` at runtime. Constants here
duplicate the corresponding `Settings` defaults; an invariant test in
`tests/risk/test_limits_match_settings.py` guarantees they cannot drift.
"""

from __future__ import annotations

from typing import Final

CONCENTRATION_CAP: Final[float] = 0.15
CYCLE_CAP: Final[float] = 0.25
PRICE_MIN: Final[float] = 0.005
PRICE_MAX: Final[float] = 0.995
MAX_ORDER_PCT_EQUITY: Final[float] = 0.50
MAX_OPEN_POSITIONS: Final[int] = 50
ORDER_RATE_LIMIT_PER_HOUR: Final[int] = 100

# Edge-proportional sizing (Phase 6b PR 4)
EDGE_THRESHOLD: Final[float] = 0.03
BASE_TRADE_FRACTION: Final[float] = 0.02
EDGE_SIZING_SCALE: Final[float] = 1.0
MAX_TRADE_FRACTION: Final[float] = 0.10
