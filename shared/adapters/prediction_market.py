"""PredictionMarketAdapter Protocol — abstracts the trading venue.

Phase-4 implementations:
- `polymarket.py` — wraps `py-clob-client` (Stream A).
- `paper_trading.py` — delegates reads to the live adapter, redirects writes
  to the `paper_trades` table (Stream A).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.models import (
    CancelResult,
    Market,
    MarketMetadata,
    Order,
    Orderbook,
    OrderResult,
    Resolution,
)


@runtime_checkable
class PredictionMarketAdapter(Protocol):
    """Read + write boundary against a prediction-market venue."""

    # Reads
    def get_markets(self, *, limit: int) -> list[Market]:
        """Return the top-`limit` liquid markets."""
        ...

    def get_orderbook(self, market_id: str) -> Orderbook:
        """Return the current best-bid/ask + ±1% depth snapshot."""
        ...

    def get_metadata(self, market_id: str) -> MarketMetadata:
        """Return resolution rules + tags for one market."""
        ...

    def get_resolution(self, market_id: str) -> Resolution | None:
        """Return the settled resolution, or None if not yet resolved."""
        ...

    # Writes
    def place_order(self, order: Order) -> OrderResult:
        """Submit an order and return the immediate result."""
        ...

    def cancel_order(self, order_id: str) -> CancelResult:
        """Cancel an open order by broker order ID."""
        ...
