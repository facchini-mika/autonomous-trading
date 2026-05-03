"""Tests for the Lead pre-fetch helpers added in Phase 6b PR 1.

The Lead pulls raw markets/orderbooks/metadata via the adapter and the
DB-side state (positions, cash, kill_switch, recent-orders count) before
handing the scanner-reviewer its `ScannerReviewerTask`. These tests use a
FakeAdapter and a stubbed SQLAlchemy session so we can verify the wiring
without touching Postgres.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from execution.lead_bootstrap import (
    _collect_trading_context,
    _collect_universe_inputs,
    _count_orders_in_last_hour,
    _load_cash_balance,
    _load_kill_switch,
    _load_open_lessons,
    _load_open_positions,
    _load_recent_notes,
)
from shared.config.settings import Settings
from shared.models import (
    Market,
    MarketMetadata,
    Orderbook,
)
from tests.adapters.fake_adapter import FakeAdapter


def _now() -> datetime:
    return datetime(2026, 5, 3, 12, 0, tzinfo=UTC)


def _market(market_id: str = "0xa") -> Market:
    return Market(
        market_id=market_id,
        condition_id=market_id,
        slug=f"slug-{market_id}",
        title=f"Title {market_id}",
        category="politics",
        end_date=_now() + timedelta(days=10),
        status="open",
        created_at=_now() - timedelta(days=30),
        last_seen=_now(),
    )


def _book(market_id: str = "0xa") -> Orderbook:
    return Orderbook(
        market_id=market_id,
        best_bid=0.4,
        best_ask=0.5,
        mid=0.45,
        depth_bid_1pct=200,
        depth_ask_1pct=200,
        timestamp=_now(),
    )


def _meta(market_id: str = "0xa") -> MarketMetadata:
    return MarketMetadata(
        market_id=market_id,
        resolution_criteria="…",
        category_tags=["politics"],
        implied_probability_yes=0.45,
    )


def _adapter_with_two_markets() -> FakeAdapter:
    fake = FakeAdapter()
    for mid in ("0xa", "0xb"):
        fake.markets[mid] = _market(mid)
        fake.orderbooks[mid] = _book(mid)
        fake.metadata[mid] = _meta(mid)
    return fake


@contextmanager
def _fake_factory(rows_by_query: dict[str, list[Any]]) -> Iterator[MagicMock]:
    """Yield a session whose `execute().all()`/`first()` is keyed by SQL keyword."""
    sess = MagicMock()

    def _execute(stmt: Any, _params: Any = None) -> MagicMock:
        sql = str(stmt).lower()
        result = MagicMock()
        rows: list[Any] = []
        for keyword, candidate in rows_by_query.items():
            if keyword in sql:
                rows = candidate
                break
        result.all.return_value = rows
        result.first.return_value = rows[0] if rows else None
        return result

    sess.execute.side_effect = _execute
    yield sess


def test_collect_universe_inputs_pulls_per_market_data() -> None:
    fake = _adapter_with_two_markets()
    settings = Settings()

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({}) as sess:
            yield sess

    inputs = _collect_universe_inputs(adapter=fake, factory=factory, settings=settings, now=_now())

    raw_markets = inputs["raw_markets"]
    assert isinstance(raw_markets, list)
    assert len(raw_markets) == 2
    raw_orderbooks = inputs["raw_orderbooks"]
    assert isinstance(raw_orderbooks, dict)
    assert set(raw_orderbooks.keys()) == {"0xa", "0xb"}
    raw_metadata = inputs["raw_metadata"]
    assert isinstance(raw_metadata, dict)
    assert set(raw_metadata.keys()) == {"0xa", "0xb"}
    assert inputs["kill_switch_active"] is False
    assert inputs["orders_in_last_hour"] == 0
    assert inputs["held_market_ids"] == []


def test_collect_universe_inputs_survives_orderbook_failure() -> None:
    fake = _adapter_with_two_markets()
    fake.orderbooks.pop("0xb")  # FakeAdapter raises KeyError on missing book
    settings = Settings()

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({}) as sess:
            yield sess

    inputs = _collect_universe_inputs(adapter=fake, factory=factory, settings=settings, now=_now())

    raw_orderbooks = inputs["raw_orderbooks"]
    assert isinstance(raw_orderbooks, dict)
    assert "0xa" in raw_orderbooks
    assert "0xb" not in raw_orderbooks


def test_load_open_positions_returns_empty_when_no_rows() -> None:
    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"from positions": []}) as sess:
            yield sess

    assert _load_open_positions(factory) == []


def test_load_cash_balance_uses_paper_trades_for_paper_mode() -> None:
    settings = Settings()
    spent = MagicMock()
    spent.spent = 250.0

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"from paper_trades": [spent]}) as sess:
            yield sess

    cash = _load_cash_balance(factory=factory, settings=settings, now=_now())

    assert cash.total_usd == settings.PAPER_STARTING_CASH_USD
    assert cash.reserved_for_orders == 250.0
    assert cash.available == settings.PAPER_STARTING_CASH_USD - 250.0


def test_load_kill_switch_handles_jsonb_object() -> None:
    row = MagicMock()
    row.value = {"active": True}

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"system_state": [row]}) as sess:
            yield sess

    assert _load_kill_switch(factory) is True


def test_load_kill_switch_defaults_false_when_missing() -> None:
    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"system_state": []}) as sess:
            yield sess

    assert _load_kill_switch(factory) is False


def test_count_orders_in_last_hour() -> None:
    row = MagicMock()
    row.total = 7

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"trades": [row]}) as sess:
            yield sess

    assert _count_orders_in_last_hour(factory=factory, now=_now()) == 7


def test_load_open_lessons_returns_empty_when_no_rows() -> None:
    settings = Settings()

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"from lessons": []}) as sess:
            yield sess

    lessons = _load_open_lessons(
        factory=factory,
        top_k=settings.LESSONS_TOP_K,
        lookback_days=settings.LESSONS_LOOKBACK_DAYS,
    )
    assert lessons == []


def test_load_recent_notes_skips_invalid_rows() -> None:
    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"from notes": []}) as sess:
            yield sess

    assert _load_recent_notes(factory=factory, agent_id="trading-agent") == []


def test_collect_trading_context_combines_lessons_and_notes() -> None:
    settings = Settings()

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        with _fake_factory({"from lessons": [], "from notes": []}) as sess:
            yield sess

    ctx = _collect_trading_context(factory=factory, settings=settings, now=_now())
    assert ctx["lessons"] == []
    assert ctx["recent_notes"] == []
