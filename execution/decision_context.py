"""ContextVar-backed `decision_id` propagation.

Stream D's risk-execution member wraps each order submission in
`with_decision(uuid)`; the PaperTradingAdapter (Phase 4a) reads
`current_decision_id.get()` to populate `paper_trades.decision_id` without
modifying the frozen `Order` Pydantic shape.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

current_decision_id: ContextVar[UUID | None] = ContextVar("current_decision_id", default=None)


@contextmanager
def with_decision(decision_id: UUID) -> Iterator[None]:
    """Bind `current_decision_id` for the duration of the block."""
    token = current_decision_id.set(decision_id)
    try:
        yield
    finally:
        current_decision_id.reset(token)


def require_decision_id() -> UUID:
    """Read the active decision_id; raise if no `with_decision` is active."""
    value = current_decision_id.get()
    if value is None:
        msg = "No active decision_id; wrap your call in `with_decision(uuid)`"
        raise RuntimeError(msg)
    return value
