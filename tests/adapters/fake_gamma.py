"""In-memory FakeGamma for tests.

Mirrors the read-only surface of `execution.gamma_client.GammaClient` that
`outcome_ingestion` depends on: `list_resolved_markets`, `get_resolution`,
and `close`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from shared.models import Resolution

if TYPE_CHECKING:
    from collections.abc import Iterator


class FakeGamma:
    """Test double for `execution.gamma_client.GammaClient`."""

    def __init__(self) -> None:
        self._resolutions: dict[str, Resolution] = {}

    def add_resolution(
        self,
        *,
        market_id: str,
        outcome: bool,
        resolved_at: datetime | None = None,
        settlement_price: float | None = None,
    ) -> None:
        self._resolutions[market_id] = Resolution(
            market_id=market_id,
            outcome=outcome,
            resolved_at=resolved_at or datetime.now(UTC),
            settlement_price=settlement_price if settlement_price is not None else (1.0 if outcome else 0.0),
            disputed=False,
        )

    def list_resolved_markets(self, *, since: datetime) -> Iterator[dict[str, Any]]:
        for market_id, resolution in self._resolutions.items():
            if resolution.resolved_at >= since:
                yield {"condition_id": market_id, "id": market_id}

    def get_resolution(self, market_id: str) -> Resolution | None:
        return self._resolutions.get(market_id)

    def close(self) -> None:
        return
