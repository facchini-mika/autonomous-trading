"""Conformance test: FakeAdapter must satisfy PredictionMarketAdapter Protocol."""

from __future__ import annotations

from datetime import UTC, datetime

from shared.adapters.prediction_market import PredictionMarketAdapter
from shared.models import Market, Order
from tests.adapters.fake_adapter import FakeAdapter


def test_fake_adapter_satisfies_protocol() -> None:
    adapter = FakeAdapter()
    assert isinstance(adapter, PredictionMarketAdapter)


def test_static_assignment_to_protocol_typed_variable() -> None:
    typed: PredictionMarketAdapter = FakeAdapter()
    assert typed is not None


def test_place_and_cancel_round_trip() -> None:
    adapter = FakeAdapter()
    order = Order(
        market_id="0xabc",
        side="yes",
        size=10.0,
        price=0.42,
        notional_usd=4.2,
        idempotency_key="test-1",
    )
    result = adapter.place_order(order)
    assert result.status == "filled"
    assert result.broker_order_id is not None

    cancel = adapter.cancel_order(result.broker_order_id)
    assert cancel.status == "cancelled"


def test_get_markets_respects_limit() -> None:
    adapter = FakeAdapter()
    now = datetime.now(UTC)
    for i in range(5):
        mid = f"m{i}"
        adapter.markets[mid] = Market(
            market_id=mid,
            condition_id=f"0x{i:064x}",
            slug=f"slug-{i}",
            title=f"Market {i}",
            category="test",
            end_date=now,
            status="open",
            created_at=now,
            last_seen=now,
        )
    assert len(adapter.get_markets(limit=3)) == 3
    assert len(adapter.get_markets(limit=100)) == 5
