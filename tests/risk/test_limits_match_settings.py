"""Drift-guard: risk.limits constants must match Settings defaults.

Production code in `risk/` is import-isolated from `shared.config`. Test
code (here) is allowed to import both and assert they agree.
"""

from __future__ import annotations

from risk.limits import (
    CONCENTRATION_CAP,
    CYCLE_CAP,
    MAX_OPEN_POSITIONS,
    MAX_ORDER_PCT_EQUITY,
    ORDER_RATE_LIMIT_PER_HOUR,
    PRICE_MAX,
    PRICE_MIN,
)
from shared.config.settings import Settings


def test_constants_match_settings_defaults() -> None:
    s = Settings()
    assert CONCENTRATION_CAP == s.CONCENTRATION_CAP
    assert CYCLE_CAP == s.CYCLE_CAP
    assert PRICE_MIN == s.ORDER_PRICE_MIN
    assert PRICE_MAX == s.ORDER_PRICE_MAX
    assert MAX_ORDER_PCT_EQUITY == s.ORDER_SANITY_MAX_PCT_EQUITY
    assert MAX_OPEN_POSITIONS == s.MAX_OPEN_POSITIONS
    assert ORDER_RATE_LIMIT_PER_HOUR == s.ORDER_RATE_LIMIT_PER_HOUR
