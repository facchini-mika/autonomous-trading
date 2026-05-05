"""Unit tests for ``execution.lead_bootstrap._python_scanner``.

Mirrors the doctrine in ``research/prompts/scanner_reviewer.md §54-124``
1:1; the Python implementation is the canonical filter+rank+assemble path
when ``UNIVERSE_FETCH_LIMIT <= TOP_K_MARKETS``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from execution.lead_bootstrap import _python_scanner
from shared.logging import configure
from shared.models import (
    CashBalance,
    Market,
    MarketMetadata,
    Orderbook,
    Position,
    ScannerReviewerTask,
    ScannerThresholds,
)


@pytest.fixture(autouse=True)
def _configure_logging() -> None:
    configure(service="test")


_NOW: datetime = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)


def _thresholds(
    *,
    max_ttr_days: int = 14,
    min_ttr_hours: int = 6,
    min_depth_1pct_usd: float = 100.0,
    max_spread: float = 0.10,
    soon_resolve_threshold_days: int = 7,
    soon_resolve_boost_multiplier: float = 1.5,
) -> ScannerThresholds:
    return ScannerThresholds(
        min_depth_1pct_usd=min_depth_1pct_usd,
        max_spread=max_spread,
        min_ttr_hours=min_ttr_hours,
        max_ttr_days=max_ttr_days,
        soon_resolve_threshold_days=soon_resolve_threshold_days,
        soon_resolve_boost_multiplier=soon_resolve_boost_multiplier,
    )


def _market(
    market_id: str,
    *,
    ttr: timedelta = timedelta(days=10),
    status: str = "open",
    ambiguity_score: float | None = None,
) -> Market:
    return Market(
        market_id=market_id,
        condition_id=market_id,
        slug="m",
        title=f"M-{market_id}",
        category="x",
        end_date=_NOW + ttr,
        status=status,
        created_at=_NOW,
        last_seen=_NOW,
        ambiguity_score=ambiguity_score,
    )


def _orderbook(
    market_id: str,
    *,
    depth: float = 200.0,
    spread: float = 0.05,
    bid: float = 0.45,
) -> Orderbook:
    return Orderbook(
        market_id=market_id,
        best_bid=bid,
        best_ask=bid + spread,
        mid=bid + spread / 2,
        depth_bid_1pct=depth,
        depth_ask_1pct=depth,
        timestamp=_NOW,
    )


def _cash(total: float = 10_000.0) -> CashBalance:
    return CashBalance(total_usd=total, available=total, reserved_for_orders=0.0, timestamp=_NOW)


def _task(
    *,
    raw_markets: list[Market],
    raw_orderbooks: dict[str, Orderbook] | None = None,
    raw_metadata: dict[str, MarketMetadata] | None = None,
    positions: list[Position] | None = None,
    cash: CashBalance | None = None,
    held_market_ids: list[str] | None = None,
    top_k: int = 50,
    thresholds: ScannerThresholds | None = None,
    kill_switch_active: bool = False,
    orders_in_last_hour: int = 0,
) -> ScannerReviewerTask:
    if raw_orderbooks is None:
        raw_orderbooks = {m.market_id: _orderbook(m.market_id) for m in raw_markets}
    if raw_metadata is None:
        raw_metadata = {}
    return ScannerReviewerTask(
        top_k=top_k,
        cycle_id="cycle-test",
        cycle_clock=_NOW.isoformat(),
        raw_markets=raw_markets,
        raw_orderbooks=raw_orderbooks,
        raw_metadata=raw_metadata,
        current_positions=positions or [],
        current_cash=cash or _cash(),
        kill_switch_active=kill_switch_active,
        held_market_ids=held_market_ids or [],
        orders_in_last_hour=orders_in_last_hour,
        thresholds=thresholds or _thresholds(),
    )


def test_filters_low_liquidity() -> None:
    """Markets with depth below the floor are dropped."""
    markets = [_market("0xa"), _market("0xb")]
    orderbooks = {
        "0xa": _orderbook("0xa", depth=200.0),
        "0xb": _orderbook("0xb", depth=50.0),  # below floor 100
    }
    out = _python_scanner(_task(raw_markets=markets, raw_orderbooks=orderbooks), clock=_NOW)
    assert [m.market_id for m in out.universe.markets] == ["0xa"]


def test_filters_wide_spread_and_long_ttr_and_status() -> None:
    """Spread, TTR, and status drops compose correctly."""
    markets = [
        _market("0xa", ttr=timedelta(days=5)),  # keeper
        _market("0xb", ttr=timedelta(days=200)),  # ttr_above_max
        _market("0xc", ttr=timedelta(hours=1)),  # ttr_below_min
        _market("0xd", ttr=timedelta(days=5), status="resolved"),  # status
    ]
    orderbooks = {m.market_id: _orderbook(m.market_id, spread=0.05 if m.market_id != "0xe" else 0.20) for m in markets}
    # Add one wide-spread market separately
    wide = _market("0xe", ttr=timedelta(days=5))
    markets.append(wide)
    orderbooks["0xe"] = _orderbook("0xe", spread=0.20)
    out = _python_scanner(_task(raw_markets=markets, raw_orderbooks=orderbooks), clock=_NOW)
    assert [m.market_id for m in out.universe.markets] == ["0xa"]


def test_held_market_always_keeps_even_when_filters_violate() -> None:
    """held_market_ids bypass every filter — operator visibility on positions."""
    markets = [
        _market("0xheld", ttr=timedelta(days=200), status="resolved"),  # would fail status + ttr
        _market("0xa", ttr=timedelta(days=5)),  # normal keeper
    ]
    out = _python_scanner(
        _task(raw_markets=markets, held_market_ids=["0xheld"]),
        clock=_NOW,
    )
    market_ids = [m.market_id for m in out.universe.markets]
    assert "0xheld" in market_ids
    assert "0xa" in market_ids


def test_held_markets_appear_first_then_descending_score() -> None:
    """Order: held first, then non-held by descending Soft-Boost score."""
    markets = [
        _market("0xlow", ttr=timedelta(days=10)),
        _market("0xhigh", ttr=timedelta(days=10)),
        _market("0xheld", ttr=timedelta(days=10)),
    ]
    orderbooks = {
        "0xlow": _orderbook("0xlow", depth=150.0),
        "0xhigh": _orderbook("0xhigh", depth=500.0),
        "0xheld": _orderbook("0xheld", depth=200.0),
    }
    out = _python_scanner(
        _task(raw_markets=markets, raw_orderbooks=orderbooks, held_market_ids=["0xheld"]),
        clock=_NOW,
    )
    ids = [m.market_id for m in out.universe.markets]
    assert ids[0] == "0xheld"  # held first
    # Then by descending score (high liquidity wins at equal TTR)
    assert ids[1:] == ["0xhigh", "0xlow"]


def test_soft_boost_multiplier_for_soon_resolving_markets() -> None:
    """Markets within ``soon_resolve_threshold_days`` get their score multiplied."""
    # "0xfar" has higher liquidity but resolves >7d out → score = 200 * 1.0 = 200
    # "0xsoon" has lower liquidity but resolves <7d → score = 150 * 1.5 = 225 → wins
    markets = [
        _market("0xfar", ttr=timedelta(days=10)),
        _market("0xsoon", ttr=timedelta(days=3)),
    ]
    orderbooks = {
        "0xfar": _orderbook("0xfar", depth=200.0),
        "0xsoon": _orderbook("0xsoon", depth=150.0),
    }
    out = _python_scanner(_task(raw_markets=markets, raw_orderbooks=orderbooks), clock=_NOW)
    assert [m.market_id for m in out.universe.markets] == ["0xsoon", "0xfar"]


def test_truncates_to_top_k() -> None:
    """Output universe length never exceeds ``top_k``."""
    markets = [_market(f"0x{i:02x}") for i in range(20)]
    out = _python_scanner(_task(raw_markets=markets, top_k=5), clock=_NOW)
    assert len(out.universe.markets) == 5
    assert len(out.universe.orderbooks) == 5


def test_portfolio_state_arithmetic_yes_position() -> None:
    """PortfolioState math: equity = cash + unrealized + realized; mark to bid."""
    markets = [_market("0xa")]
    orderbooks = {"0xa": _orderbook("0xa", bid=0.60, spread=0.04, depth=500.0)}
    position = Position(
        market_id="0xa",
        side="yes",
        size=100.0,
        avg_price=0.50,
        unrealized_pnl=0.0,  # ignored — recomputed
        realized_pnl=5.0,
        opened_at=_NOW,
        last_updated=_NOW,
    )
    out = _python_scanner(
        _task(raw_markets=markets, raw_orderbooks=orderbooks, positions=[position], held_market_ids=["0xa"]),
        clock=_NOW,
    )
    portfolio = out.portfolio_state
    # mark to bid: (0.60 - 0.50) * 100 = 10.0
    assert portfolio.unrealized_pnl == pytest.approx(10.0)
    assert portfolio.realized_pnl == pytest.approx(5.0)
    assert portfolio.gross_exposure_usd == pytest.approx(50.0)  # 100 * 0.50
    # equity = cash 10_000 + 10 unrealized + 5 realized = 10_015
    assert portfolio.equity == pytest.approx(10_015.0)
    assert portfolio.kill_switch_active is False
    assert portfolio.orders_in_last_hour == 0


def test_portfolio_state_no_position_uses_inverse_ask() -> None:
    """``side='no'``: unrealized = ((1 - best_ask) - avg_price) * size."""
    markets = [_market("0xa")]
    orderbooks = {"0xa": _orderbook("0xa", bid=0.30, spread=0.05, depth=500.0)}  # ask=0.35
    position = Position(
        market_id="0xa",
        side="no",
        size=100.0,
        avg_price=0.40,  # bought NO at 0.40, current 1-ask = 0.65 → +0.25 per unit
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        opened_at=_NOW,
        last_updated=_NOW,
    )
    out = _python_scanner(
        _task(raw_markets=markets, raw_orderbooks=orderbooks, positions=[position], held_market_ids=["0xa"]),
        clock=_NOW,
    )
    # ((1 - 0.35) - 0.40) * 100 = 25.0
    assert out.portfolio_state.unrealized_pnl == pytest.approx(25.0)


def test_position_without_orderbook_contributes_zero() -> None:
    """Doctrine §114-115: positions whose market lacks an orderbook contribute 0."""
    position = Position(
        market_id="0xghost",
        side="yes",
        size=100.0,
        avg_price=0.50,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
        opened_at=_NOW,
        last_updated=_NOW,
    )
    out = _python_scanner(
        _task(raw_markets=[], raw_orderbooks={}, positions=[position], held_market_ids=["0xghost"]),
        clock=_NOW,
    )
    assert out.portfolio_state.unrealized_pnl == 0.0


def test_dispute_history_drops_market() -> None:
    """Doctrine §65-66: non-empty dispute_history disqualifies a market."""
    markets = [_market("0xa"), _market("0xb")]
    metadata = {
        "0xa": MarketMetadata(
            market_id="0xa",
            resolution_criteria="...",
            implied_probability_yes=0.5,
            dispute_history=None,
        ),
        "0xb": MarketMetadata(
            market_id="0xb",
            resolution_criteria="...",
            implied_probability_yes=0.5,
            dispute_history="resolved against majority on 2025-12",
        ),
    }
    out = _python_scanner(_task(raw_markets=markets, raw_metadata=metadata), clock=_NOW)
    assert [m.market_id for m in out.universe.markets] == ["0xa"]


def test_kill_switch_passes_through() -> None:
    """``kill_switch_active`` is mirrored from task to portfolio."""
    out = _python_scanner(_task(raw_markets=[], kill_switch_active=True), clock=_NOW)
    assert out.portfolio_state.kill_switch_active is True
