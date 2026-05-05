"""Tests for ``execution.lead_bootstrap._enforce_universe_invariants``.

The helper is the defensive post-filter that catches scanner-LLM
non-compliance. Cycle-7 emitted markets at TTR=239d despite the
``max_ttr_days=14`` threshold; this filter drops them client-side
before the trading-agent ever sees them. Held markets are always
preserved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from execution.lead_bootstrap import _enforce_universe_invariants
from shared.logging import configure
from shared.models import Market, Orderbook, ScannerThresholds, Universe


@pytest.fixture(autouse=True)
def _configure_logging() -> None:
    """Initialise structlog so capsys can pick up the JSON log lines."""
    configure(service="test")


_NOW: datetime = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)


def _thresholds(
    *,
    max_ttr_days: int = 14,
    min_ttr_hours: int = 6,
    min_depth_1pct_usd: float = 100.0,
    max_spread: float = 0.10,
) -> ScannerThresholds:
    return ScannerThresholds(
        min_depth_1pct_usd=min_depth_1pct_usd,
        max_spread=max_spread,
        min_ttr_hours=min_ttr_hours,
        max_ttr_days=max_ttr_days,
        soon_resolve_threshold_days=7,
        soon_resolve_boost_multiplier=1.5,
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
) -> Orderbook:
    return Orderbook(
        market_id=market_id,
        best_bid=0.45,
        best_ask=0.45 + spread,
        mid=0.475,
        depth_bid_1pct=depth,
        depth_ask_1pct=depth,
        timestamp=_NOW,
    )


def _universe(markets: list[Market]) -> Universe:
    return Universe(
        markets=markets,
        orderbooks={m.market_id: _orderbook(m.market_id) for m in markets},
        timestamp=_NOW,
    )


def test_keeps_compliant_market() -> None:
    universe = _universe([_market("0xa", ttr=timedelta(days=10))])
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert [m.market_id for m in out.markets] == ["0xa"]


def test_drops_long_ttr_markets() -> None:
    universe = _universe(
        [
            _market("0xa", ttr=timedelta(days=10)),
            _market("0xb", ttr=timedelta(days=20)),
        ]
    )
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert [m.market_id for m in out.markets] == ["0xa"]


def test_drops_short_ttr_markets() -> None:
    universe = _universe(
        [
            _market("0xa", ttr=timedelta(hours=3)),
            _market("0xb", ttr=timedelta(days=10)),
        ]
    )
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert [m.market_id for m in out.markets] == ["0xb"]


def test_keeps_held_market_even_when_ttr_violates() -> None:
    """Held positions must remain visible regardless of any filter."""
    universe = _universe(
        [
            _market("0xa", ttr=timedelta(days=200)),
            _market("0xb", ttr=timedelta(days=20)),
        ]
    )
    out = _enforce_universe_invariants(
        universe=universe,
        thresholds=_thresholds(),
        held_market_ids={"0xa"},
        clock=_NOW,
    )
    assert [m.market_id for m in out.markets] == ["0xa"]
    assert "0xa" in out.orderbooks


def test_drops_low_liquidity() -> None:
    market_a = _market("0xa", ttr=timedelta(days=5))
    universe = Universe(
        markets=[market_a],
        orderbooks={"0xa": _orderbook("0xa", depth=50.0)},
        timestamp=_NOW,
    )
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert out.markets == []


def test_drops_wide_spread() -> None:
    market_a = _market("0xa", ttr=timedelta(days=5))
    universe = Universe(
        markets=[market_a],
        orderbooks={"0xa": _orderbook("0xa", spread=0.20)},
        timestamp=_NOW,
    )
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert out.markets == []


def test_drops_non_open_status() -> None:
    universe = _universe([_market("0xa", ttr=timedelta(days=5), status="resolved")])
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert out.markets == []


def test_drops_high_ambiguity() -> None:
    universe = _universe([_market("0xa", ttr=timedelta(days=5), ambiguity_score=0.7)])
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert out.markets == []


def test_drops_market_with_missing_orderbook() -> None:
    market_a = _market("0xa", ttr=timedelta(days=5))
    universe = Universe(markets=[market_a], orderbooks={}, timestamp=_NOW)
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert out.markets == []


def test_logs_scanner_violation_per_drop(capsys: pytest.CaptureFixture[str]) -> None:
    universe = _universe(
        [
            _market("0xa", ttr=timedelta(days=20)),
            _market("0xb", ttr=timedelta(days=200)),
        ]
    )
    _enforce_universe_invariants(
        universe=universe,
        thresholds=_thresholds(),
        held_market_ids=set(),
        clock=_NOW,
    )
    captured = capsys.readouterr().out
    assert captured.count("scanner_violation") == 2
    assert "ttr_above_max" in captured
    assert "scanner_post_filter_done" in captured


def test_orderbooks_are_subset_of_kept_markets() -> None:
    universe = _universe(
        [
            _market("0xa", ttr=timedelta(days=10)),
            _market("0xb", ttr=timedelta(days=20)),
        ]
    )
    out = _enforce_universe_invariants(universe=universe, thresholds=_thresholds(), held_market_ids=set(), clock=_NOW)
    assert set(out.orderbooks.keys()) == {"0xa"}
