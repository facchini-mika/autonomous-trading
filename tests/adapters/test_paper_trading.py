"""Tests for PaperTradingAdapter (mocked session + FakeAdapter as live)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from shared.adapters.paper_trading import PAPER_BROKER_PREFIX, PaperTradingAdapter
from shared.adapters.prediction_market import PredictionMarketAdapter
from shared.models import (
    Market,
    MarketMetadata,
    Order,
    Orderbook,
    Resolution,
)
from tests.adapters.fake_adapter import FakeAdapter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def session() -> MagicMock:
    return MagicMock()


@pytest.fixture
def session_factory(session: MagicMock):
    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield session

    return factory


@pytest.fixture
def fake_live() -> FakeAdapter:
    fake = FakeAdapter()
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    fake.markets["0xa"] = Market(
        market_id="0xa",
        condition_id="0xa",
        slug="a",
        title="A",
        category="x",
        end_date=now,
        status="open",
        created_at=now,
        last_seen=now,
    )
    fake.orderbooks["0xa"] = Orderbook(
        market_id="0xa",
        best_bid=0.4,
        best_ask=0.5,
        mid=0.45,
        depth_bid_1pct=10,
        depth_ask_1pct=10,
        timestamp=now,
    )
    fake.metadata["0xa"] = MarketMetadata(
        market_id="0xa",
        resolution_criteria="x",
        implied_probability_yes=0.5,
    )
    fake.resolutions["0xb"] = Resolution(
        market_id="0xb",
        outcome=True,
        resolved_at=now,
        settlement_price=1.0,
    )
    return fake


@pytest.fixture
def decision_id() -> UUID:
    return uuid4()


@pytest.fixture
def adapter(
    fake_live: FakeAdapter,
    session_factory,
    decision_id: UUID,
    fixed_now: datetime,
) -> PaperTradingAdapter:
    return PaperTradingAdapter(
        live_adapter=fake_live,
        session_factory=session_factory,
        decision_id_provider=lambda: decision_id,
        clock=lambda: fixed_now,
    )


def test_satisfies_protocol(adapter: PaperTradingAdapter) -> None:
    assert isinstance(adapter, PredictionMarketAdapter)


def test_reads_delegate_to_live(adapter: PaperTradingAdapter) -> None:
    assert len(adapter.get_markets(limit=5)) == 1
    book = adapter.get_orderbook("0xa")
    assert book.best_bid == pytest.approx(0.4)
    assert adapter.get_metadata("0xa").implied_probability_yes == pytest.approx(0.5)
    assert adapter.get_resolution("0xb") is not None
    assert adapter.get_resolution("nonexistent") is None


def test_place_order_returns_filled_at_requested_price(
    adapter: PaperTradingAdapter,
    fixed_now: datetime,
) -> None:
    order = Order(market_id="0xa", side="yes", size=2.0, price=0.45, notional_usd=0.9, idempotency_key="k1")
    result = adapter.place_order(order)
    assert result.status == "filled"
    assert result.fill_price == pytest.approx(0.45)
    assert result.filled_size == pytest.approx(2.0)


def test_place_order_broker_id_starts_with_paper_prefix(adapter: PaperTradingAdapter) -> None:
    order = Order(market_id="0xa", side="yes", size=1, price=0.5, notional_usd=0.5, idempotency_key="k2")
    result = adapter.place_order(order)
    assert result.broker_order_id is not None
    assert result.broker_order_id.startswith(PAPER_BROKER_PREFIX)


def test_place_order_writes_paper_trades_insert(
    adapter: PaperTradingAdapter,
    session: MagicMock,
) -> None:
    order = Order(market_id="0xa", side="yes", size=3, price=0.4, notional_usd=1.2, idempotency_key="k3")
    adapter.place_order(order)
    assert session.execute.call_count == 1
    sql = str(session.execute.call_args.args[0])
    assert "INSERT INTO paper_trades" in sql
    assert "trades " not in sql.split("paper_trades", maxsplit=1)[0]


def test_place_order_passes_decision_id(
    fake_live: FakeAdapter,
    session_factory,
    fixed_now: datetime,
) -> None:
    seen: dict[str, str] = {}

    @contextmanager
    def capture_factory():
        sess = MagicMock()
        original = sess.execute

        def remember(stmt, params=None):
            if params:
                seen.update({k: str(v) for k, v in params.items()})
            return original(stmt, params)

        sess.execute = remember
        yield sess

    decision = uuid4()
    adapter = PaperTradingAdapter(
        live_adapter=fake_live,
        session_factory=capture_factory,
        decision_id_provider=lambda: decision,
        clock=lambda: fixed_now,
    )
    adapter.place_order(
        Order(market_id="0xa", side="yes", size=1, price=0.5, notional_usd=0.5, idempotency_key="k"),
    )
    assert seen["decision_id"] == str(decision)


def test_place_order_does_not_write_to_live(
    adapter: PaperTradingAdapter,
    fake_live: FakeAdapter,
) -> None:
    adapter.place_order(
        Order(market_id="0xa", side="yes", size=1, price=0.5, notional_usd=0.5, idempotency_key="k4"),
    )
    assert fake_live.placed_orders == []


def test_cancel_order_marks_paper_row(
    fake_live: FakeAdapter,
    decision_id: UUID,
    fixed_now: datetime,
) -> None:
    sess = MagicMock()
    sess.execute.return_value.first.return_value = (3.5,)

    @contextmanager
    def factory():
        yield sess

    adapter = PaperTradingAdapter(
        live_adapter=fake_live,
        session_factory=factory,
        decision_id_provider=lambda: decision_id,
        clock=lambda: fixed_now,
    )
    res = adapter.cancel_order("paper-abc")
    assert res.status == "cancelled"
    assert res.cancelled_size == pytest.approx(3.5)


def test_cancel_order_not_found(
    fake_live: FakeAdapter,
    decision_id: UUID,
    fixed_now: datetime,
) -> None:
    sess = MagicMock()
    sess.execute.return_value.first.return_value = None

    @contextmanager
    def factory():
        yield sess

    adapter = PaperTradingAdapter(
        live_adapter=fake_live,
        session_factory=factory,
        decision_id_provider=lambda: decision_id,
        clock=lambda: fixed_now,
    )
    res = adapter.cancel_order("paper-missing")
    assert res.status == "not_found"
