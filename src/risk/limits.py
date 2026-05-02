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
