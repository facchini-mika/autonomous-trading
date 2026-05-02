"""Pure PnL math for resolved markets — no DB, no network.

Realized PnL conventions (per specs/trading.md):
- A YES position pays $1 per share if YES resolves, $0 otherwise.
- A NO position pays $1 per share if NO resolves (i.e. YES does NOT).
- Fees are subtracted from PnL; gas is treated separately for paper trades
  (always 0) and real trades (passed in).
"""

from __future__ import annotations


def realized_pnl(
    *,
    side: str,
    size: float,
    entry_price: float,
    outcome_yes: bool,
    fees: float = 0.0,
    gas: float = 0.0,
) -> float:
    """Compute realized PnL for a resolved binary-market position.

    `side` is 'yes' or 'no'; `size` is in shares; `entry_price` ∈ [0, 1] is
    the per-share buy price; `outcome_yes` is True iff YES won.
    """
    if size < 0:
        msg = "size must be >= 0"
        raise ValueError(msg)
    if not 0.0 <= entry_price <= 1.0:
        msg = "entry_price must be in [0, 1]"
        raise ValueError(msg)
    if side not in {"yes", "no"}:
        msg = f"side must be 'yes' or 'no', got {side!r}"
        raise ValueError(msg)

    payout_per_share = 1.0 if (side == "yes") == outcome_yes else 0.0
    gross = size * (payout_per_share - entry_price)
    return gross - fees - gas


def mark_to_market_bid(
    *,
    side: str,
    size: float,
    entry_price: float,
    current_bid: float,
) -> float:
    """Conservative MTM: value an open position at the prevailing bid.

    For a YES position, bid is the price someone will pay you for a YES share.
    For a NO position, the conservative price is `1 - current_bid` for the
    opposing YES bid (since NO and YES sum to ~1.0 in healthy markets).
    """
    if size < 0:
        msg = "size must be >= 0"
        raise ValueError(msg)
    if not 0.0 <= entry_price <= 1.0:
        msg = "entry_price must be in [0, 1]"
        raise ValueError(msg)
    if not 0.0 <= current_bid <= 1.0:
        msg = "current_bid must be in [0, 1]"
        raise ValueError(msg)
    if side not in {"yes", "no"}:
        msg = f"side must be 'yes' or 'no', got {side!r}"
        raise ValueError(msg)

    exit_price = current_bid if side == "yes" else (1.0 - current_bid)
    return size * (exit_price - entry_price)
