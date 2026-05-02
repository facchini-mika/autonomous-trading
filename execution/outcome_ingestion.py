"""Daily outcome-ingestion script.

Reads resolved markets from Gamma, sets `predictions.outcome` and
`realized_pnl`, propagates PnL to `trades`, `paper_trades`, and `positions`.
Idempotent: only updates rows where the target column is still NULL.

Authority boundary: binds to the `outcome_ingestion` Postgres role (UPDATE
rights on a column subset only). No signing keys, no live trading API.

Run via:
    uv run python -m execution.outcome_ingestion

Or programmatically: `run_once(gamma=GammaClient(), session_factory=...)`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from execution.outcome_math import realized_pnl
from shared.db import get_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

    from execution.gamma_client import GammaClient

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 7
HIGH_WATER_MARK_KEY = "last_outcome_ingestion_at"


def run_once(
    *,
    gamma: GammaClient,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, int]:
    """Single ingestion pass; returns counts of {predictions, trades, positions}."""
    factory = session_factory or _default_factory
    now = (clock or _utcnow)()
    counts = {"predictions": 0, "trades": 0, "paper_trades": 0, "positions": 0}

    since = _read_high_water_mark(factory, fallback=now - timedelta(days=lookback_days))
    logger.info("outcome_ingestion: scanning resolved markets since %s", since.isoformat())

    for raw in gamma.list_resolved_markets(since=since):
        market_id = str(raw.get("condition_id") or raw.get("id") or "")
        if not market_id:
            continue
        resolution = gamma.get_resolution(market_id)
        if resolution is None:
            continue
        outcome_yes = resolution.outcome
        with factory() as session:
            counts["predictions"] += _set_predictions_outcome(session, market_id, outcome_yes=outcome_yes)
            counts["trades"] += _set_trades_pnl(session, market_id, outcome_yes=outcome_yes, table="trades")
            counts["paper_trades"] += _set_trades_pnl(
                session,
                market_id,
                outcome_yes=outcome_yes,
                table="paper_trades",
            )
            counts["positions"] += _close_positions(session, market_id, outcome_yes=outcome_yes)

    _write_high_water_mark(factory, now)
    logger.info("outcome_ingestion done: %s", counts)
    return counts


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("outcome_ingestion")


def _read_high_water_mark(
    factory: Callable[[], AbstractContextManager[Session]],
    *,
    fallback: datetime,
) -> datetime:
    with factory() as session:
        row = session.execute(
            text("SELECT value FROM system_state WHERE key = :k"),
            {"k": HIGH_WATER_MARK_KEY},
        ).first()
    if row is None or row[0] is None:
        return fallback
    raw_value: Any = row[0]
    ts = raw_value.get("ts") if isinstance(raw_value, dict) else raw_value
    if not isinstance(ts, str):
        return fallback
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return fallback


def _write_high_water_mark(
    factory: Callable[[], AbstractContextManager[Session]],
    ts: datetime,
) -> None:
    with factory() as session:
        session.execute(
            text(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (:k, jsonb_build_object('ts', :v), NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = NOW()
                """,
            ),
            {"k": HIGH_WATER_MARK_KEY, "v": ts.isoformat()},
        )


def _set_predictions_outcome(session: Session, market_id: str, *, outcome_yes: bool) -> int:
    result = session.execute(
        text(
            """
            UPDATE predictions
            SET outcome = :outcome
            WHERE market_id = :market_id
              AND outcome IS NULL
            """,
        ),
        {"market_id": market_id, "outcome": outcome_yes},
    )
    return int(getattr(result, "rowcount", 0) or 0)


def _set_trades_pnl(session: Session, market_id: str, *, outcome_yes: bool, table: str) -> int:
    rows = session.execute(
        text(
            f"""
            SELECT id, side, size, price, fees, gas
            FROM {table}
            WHERE market_id = :market_id
              AND realized_pnl IS NULL
              AND status IN ('filled', 'partial')
            """,
        ),
        {"market_id": market_id},
    ).all()
    total_pnl_by_position: dict[tuple[str, str], float] = {}
    for row in rows:
        pnl = realized_pnl(
            side=str(row.side),
            size=float(row.size),
            entry_price=float(row.price),
            outcome_yes=outcome_yes,
            fees=float(row.fees),
            gas=float(row.gas),
        )
        session.execute(
            text(f"UPDATE {table} SET realized_pnl = :pnl WHERE id = :id"),
            {"pnl": pnl, "id": row.id},
        )
        total_pnl_by_position[(market_id, str(row.side))] = (
            total_pnl_by_position.get((market_id, str(row.side)), 0.0) + pnl
        )
    return len(rows)


def _close_positions(session: Session, market_id: str, *, outcome_yes: bool) -> int:
    rows = session.execute(
        text(
            """
            SELECT market_id, side, size, avg_price
            FROM positions
            WHERE market_id = :market_id
              AND status = 'open'
            """,
        ),
        {"market_id": market_id},
    ).all()
    for row in rows:
        pnl = realized_pnl(
            side=str(row.side),
            size=float(row.size),
            entry_price=float(row.avg_price),
            outcome_yes=outcome_yes,
        )
        session.execute(
            text(
                """
                UPDATE positions
                SET status = 'closed',
                    realized_pnl = :pnl,
                    last_updated = NOW()
                WHERE market_id = :market_id AND side = :side
                """,
            ),
            {"pnl": pnl, "market_id": market_id, "side": row.side},
        )
    return len(rows)


if __name__ == "__main__":
    from execution.gamma_client import GammaClient as _GammaClient

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = _GammaClient()
    try:
        run_once(gamma=client)
    finally:
        client.close()
