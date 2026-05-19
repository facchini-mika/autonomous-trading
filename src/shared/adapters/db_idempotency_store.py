"""Postgres-backed `IdempotencyStore` for `PolymarketAdapter.place_order`.

Replaces the in-memory cache so a process crash between ``POST /order``
and the subsequent ``put`` cannot let the next cycle re-submit the same
logical order. See migration ``0009_order_attempts`` and
``specs/data_infrastructure.md §2`` for the failure mode this guards.

Three-step protocol used by ``PolymarketAdapter.place_order``:

1. ``get(key)`` — return any cached terminal result. Pending (unfinished)
   rows do *not* surface here; the adapter must treat them as conflicts.
2. ``reserve(key, cycle_id, decision_id)`` — ``INSERT ... ON CONFLICT
   DO NOTHING``. Returns ``True`` on a fresh reservation, ``False`` if a
   row already exists (terminal *or* in-flight). On ``False`` the adapter
   raises ``IdempotencyConflict`` and skips the order; the operator
   reconciles via the orphan log.
3. ``put(key, result)`` — finalise the row with the terminal status and
   ``finished_at`` so future ``get(key)`` calls short-circuit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from shared.models import OrderResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session


class DbIdempotencyStore:
    """Durable idempotency cache backed by the ``order_attempts`` table."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]],
    ) -> None:
        self._factory = session_factory

    def get(self, key: str) -> OrderResult | None:
        """Return the terminal result for ``key`` if one exists, else ``None``.

        Pending rows (``finished_at IS NULL``) are intentionally not
        returned: an in-flight or crashed-mid-submit attempt should
        surface as a reserve-conflict at the next ``reserve`` call, not
        silently re-use an unknown CLOB state.
        """
        with self._factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT status, broker_order_id, fill_price, filled_size, fees
                    FROM order_attempts
                    WHERE idempotency_key = :key
                      AND finished_at IS NOT NULL
                    """,
                ),
                {"key": key},
            ).first()
        if row is None:
            return None
        return OrderResult(
            status=row.status,
            broker_order_id=row.broker_order_id,
            fill_price=float(row.fill_price) if row.fill_price is not None else None,
            filled_size=float(row.filled_size or 0.0),
            fees=float(row.fees or 0.0),
        )

    def reserve(self, key: str, *, cycle_id: str, decision_id: str) -> bool:
        """Reserve ``key`` for a first-time submission attempt.

        Returns ``True`` if the row was inserted (fresh attempt),
        ``False`` if it already exists (terminal or in-flight).
        """
        with self._factory() as session:
            result = session.execute(
                text(
                    """
                    INSERT INTO order_attempts (idempotency_key, cycle_id, decision_id, status)
                    VALUES (:key, :cycle_id, :decision_id, 'pending')
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                ),
                {"key": key, "cycle_id": cycle_id, "decision_id": decision_id},
            )
            return int(getattr(result, "rowcount", 0) or 0) == 1

    def put(
        self,
        key: str,
        value: OrderResult,
        *,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Finalise the reserved row with the terminal CLOB result."""
        with self._factory() as session:
            session.execute(
                text(
                    """
                    UPDATE order_attempts
                    SET status = :status,
                        broker_order_id = :broker_order_id,
                        fill_price = :fill_price,
                        filled_size = :filled_size,
                        fees = :fees,
                        error_class = :error_class,
                        error_message = :error_message,
                        finished_at = now()
                    WHERE idempotency_key = :key
                    """,
                ),
                {
                    "key": key,
                    "status": value.status,
                    "broker_order_id": value.broker_order_id,
                    "fill_price": value.fill_price,
                    "filled_size": value.filled_size,
                    "fees": value.fees,
                    "error_class": error_class,
                    "error_message": error_message,
                },
            )
