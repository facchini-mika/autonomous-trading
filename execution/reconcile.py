"""Reconciliation: flag trades whose internal PnL diverges from Gamma-implied PnL.

Diff > `Settings.RECONCILIATION_DIFF_USD` (0.50) → INSERT into `system_state`
with key `reconciliation_flag:<trade_id>`. **Does NOT auto-trigger kill-switch**;
operator inspects flagged rows and decides.

Authority boundary: same as outcome_ingestion (read predictions/trades, UPDATE
system_state only).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from execution.outcome_math import realized_pnl
from shared.config.settings import Settings, reconciliation_flag_key
from shared.db import get_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def reconcile_market(
    market_id: str,
    *,
    outcome_yes: bool,
    settings: Settings | None = None,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
) -> int:
    """Compare every filled trade for `market_id` against the Gamma-implied PnL.

    Returns the number of flags written. `outcome_yes` should match
    `Resolution.outcome` from Gamma. The internal PnL stored in the trade row
    must already have been written by outcome_ingestion.
    """
    s = settings or Settings()
    threshold = s.RECONCILIATION_DIFF_USD
    factory = session_factory or _default_factory

    with factory() as session:
        rows = session.execute(
            text(
                """
                SELECT id, side, size, price, fees, gas, realized_pnl
                FROM trades
                WHERE market_id = :market_id
                  AND realized_pnl IS NOT NULL
                """,
            ),
            {"market_id": market_id},
        ).all()
        flagged = 0
        for row in rows:
            expected = realized_pnl(
                side=str(row.side),
                size=float(row.size),
                entry_price=float(row.price),
                outcome_yes=outcome_yes,
                fees=float(row.fees),
                gas=float(row.gas),
            )
            actual = float(row.realized_pnl)
            delta = abs(expected - actual)
            if delta > threshold:
                _write_flag(session, str(row.id), expected=expected, actual=actual, delta=delta)
                flagged += 1
                logger.warning(
                    "reconciliation flag for trade %s: expected=%.4f actual=%.4f delta=%.4f",
                    row.id,
                    expected,
                    actual,
                    delta,
                )
    return flagged


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("outcome_ingestion")


def _write_flag(session: Session, trade_id: str, *, expected: float, actual: float, delta: float) -> None:
    payload = {
        "expected_pnl": expected,
        "actual_pnl": actual,
        "delta": delta,
    }
    session.execute(
        text(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (:k, CAST(:v AS jsonb), NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value, updated_at = NOW()
            """,
        ),
        {"k": reconciliation_flag_key(trade_id), "v": json.dumps(payload)},
    )
