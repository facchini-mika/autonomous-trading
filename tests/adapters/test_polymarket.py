"""Tests for PolymarketAdapter (mocked py-clob-client-v2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from shared.adapters.polymarket import (
    InMemoryIdempotencyStore,
    PolymarketAdapter,
    PolymarketPermanentError,
    PolymarketRateLimitError,
    PolymarketTransientError,
    _backoff_for,
    _to_clob_order_args,
)
from shared.adapters.prediction_market import PredictionMarketAdapter
from shared.models import Order


@pytest.fixture
def fake_clob() -> MagicMock:
    return MagicMock()


@pytest.fixture
def adapter(fake_clob: MagicMock) -> PolymarketAdapter:
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    return PolymarketAdapter(
        key_provider=MagicMock(),
        host="http://test",
        chain_id=137,
        clock=lambda: fixed_now,
        client_factory=lambda **_kw: fake_clob,
    )


def test_satisfies_protocol_runtime(adapter: PolymarketAdapter) -> None:
    assert isinstance(adapter, PredictionMarketAdapter)


def test_satisfies_protocol_static(adapter: PolymarketAdapter) -> None:
    typed: PredictionMarketAdapter = adapter
    assert typed is adapter


def test_get_markets_passes_limit(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.get_markets.return_value = {
        "data": [
            {
                "condition_id": f"0x{i:064x}",
                "id": str(i),
                "market_slug": f"slug-{i}",
                "question": f"Q{i}",
                "category": "test",
                "end_date_iso": "2026-12-31T00:00:00Z",
                "tokens": [{"outcome": "yes", "price": 0.5}],
            }
            for i in range(10)
        ],
    }
    markets = adapter.get_markets(limit=3)
    assert len(markets) == 3


def test_get_orderbook_maps_response(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.get_order_book.return_value = {
        "bids": [{"price": 0.45, "size": 100}, {"price": 0.44, "size": 50}],
        "asks": [{"price": 0.46, "size": 80}, {"price": 0.47, "size": 40}],
    }
    book = adapter.get_orderbook("0xabc")
    assert book.market_id == "0xabc"
    assert book.best_bid == pytest.approx(0.45)
    assert book.best_ask == pytest.approx(0.46)
    assert book.mid == pytest.approx(0.455)
    assert book.depth_bid_1pct >= 100
    assert book.depth_ask_1pct >= 80


def test_get_orderbook_handles_empty_book(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.get_order_book.return_value = {"bids": [], "asks": []}
    book = adapter.get_orderbook("0xempty")
    assert book.depth_bid_1pct == 0.0
    assert book.depth_ask_1pct == 0.0


def test_get_metadata_extracts_yes_token_price(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.get_market.return_value = {
        "description": "Will it rain?",
        "tags": ["weather"],
        "tokens": [
            {"outcome": "yes", "price": 0.72},
            {"outcome": "no", "price": 0.28},
        ],
    }
    meta = adapter.get_metadata("0xabc")
    assert meta.implied_probability_yes == pytest.approx(0.72)
    assert meta.category_tags == ["weather"]


def test_get_resolution_returns_none_for_unresolved(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.get_market.return_value = {"closed": False}
    assert adapter.get_resolution("0xabc") is None


def test_get_resolution_parses_closed(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.get_market.return_value = {
        "closed": True,
        "outcome": "yes",
        "resolved_at": "2026-04-30T00:00:00Z",
        "settlement_price": 1.0,
    }
    res = adapter.get_resolution("0xabc")
    assert res is not None
    assert res.outcome is True


def test_place_order_idempotency_returns_cached(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.create_order.return_value = {"signed": True}
    fake_clob.post_order.return_value = {"status": "matched", "order_id": "abc", "price": 0.5, "size_matched": 1}

    order = _make_order("idem-1")
    first = adapter.place_order(order)
    second = adapter.place_order(order)

    assert first == second
    assert fake_clob.post_order.call_count == 1


def test_place_order_filled_status(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.post_order.return_value = {"status": "matched", "order_id": "ok", "price": 0.5, "size_matched": 2}
    fake_clob.create_order.return_value = {}
    res = adapter.place_order(_make_order("k1"))
    assert res.status == "filled"
    assert res.broker_order_id == "ok"
    assert res.filled_size == pytest.approx(2.0)


def test_place_order_retry_on_transient_then_succeeds(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.create_order.return_value = {}
    fake_clob.post_order.side_effect = [
        PolymarketTransientError("503"),
        PolymarketTransientError("503"),
        {"status": "matched", "order_id": "x", "price": 0.5},
    ]
    res = adapter.place_order(_make_order("k-retry"))
    assert res.status == "filled"
    assert fake_clob.post_order.call_count == 3


def test_place_order_no_retry_on_permanent_returns_rejected(
    adapter: PolymarketAdapter,
    fake_clob: MagicMock,
) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.create_order.return_value = {}
    fake_clob.post_order.side_effect = PolymarketPermanentError("400 invalid signature")
    res = adapter.place_order(_make_order("k-perm"))
    assert res.status == "rejected"
    assert fake_clob.post_order.call_count == 1


def test_place_order_retry_exhaustion_raises(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.create_order.return_value = {}
    fake_clob.post_order.side_effect = PolymarketTransientError("503")
    with pytest.raises(PolymarketTransientError):
        adapter.place_order(_make_order("k-exhaust"))
    assert fake_clob.post_order.call_count == 3


def test_rate_limit_error_is_transient_and_retried(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.create_order.return_value = {}
    fake_clob.post_order.side_effect = [
        PolymarketRateLimitError("429"),
        {"status": "matched", "order_id": "x"},
    ]
    res = adapter.place_order(_make_order("k-429"))
    assert res.status == "filled"


def test_cancel_order_maps_not_found(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.cancel_order.side_effect = PolymarketPermanentError("404 not found")
    res = adapter.cancel_order("missing")
    assert res.status == "not_found"


def test_cancel_order_success(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.cancel_order.return_value = {"canceled": [{"order_id": "ok", "size": 5}]}
    res = adapter.cancel_order("ok")
    assert res.status == "cancelled"
    assert res.cancelled_size == pytest.approx(5.0)


def test_init_calls_api_creds_only_once(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.create_order.return_value = {}
    fake_clob.post_order.return_value = {"status": "matched", "order_id": "x"}
    adapter.place_order(_make_order("k-a"))
    adapter.place_order(_make_order("k-b"))
    assert fake_clob.create_or_derive_api_key.call_count == 1


def test_idempotency_store_get_put() -> None:
    store = InMemoryIdempotencyStore()
    assert store.get("missing") is None
    from shared.models import OrderResult

    res = OrderResult(status="filled", broker_order_id="x", fill_price=0.5, filled_size=1.0)
    store.put("k", res)
    assert store.get("k") == res


def test_backoff_increases_with_attempt() -> None:
    a0 = _backoff_for(0)
    a1 = _backoff_for(1)
    a2 = _backoff_for(2)
    assert 0.0 <= a0 <= 1.0
    assert a1 > a0 / 2
    assert a2 > a1 / 2


def test_place_order_parses_v2_response_shape(adapter: PolymarketAdapter, fake_clob: MagicMock) -> None:
    fake_clob.create_or_derive_api_key.return_value = MagicMock()
    fake_clob.create_order.return_value = {}
    fake_clob.post_order.return_value = {
        "errorMsg": "",
        "orderID": "<redacted>",
        "takingAmount": "5",
        "makingAmount": "2.55",
        "status": "matched",
        "transactionsHashes": ["0xdeadbeef"],
    }
    res = adapter.place_order(_make_order("v2-shape", side="yes"))
    assert res.status == "filled"
    assert res.broker_order_id == "<redacted>"
    assert res.filled_size == pytest.approx(5.0)
    assert res.fill_price == pytest.approx(0.51)


def test_estimate_fee_reads_market_fee_rate(fake_clob: MagicMock) -> None:
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    adapter = PolymarketAdapter(
        key_provider=MagicMock(),
        host="http://test",
        chain_id=137,
        clock=lambda: fixed_now,
        client_factory=lambda **_kw: fake_clob,
        default_fee_rate_bps=200,
    )
    fake_clob.get_market.return_value = {"fee_rate_bps": 50}
    order = Order(
        market_id="0xmarket",
        side="yes",
        size=10.0,
        price=0.50,
        notional_usd=5.0,
        idempotency_key="fee-1",
    )
    fee = adapter.estimate_fee(order)
    assert fee == pytest.approx(0.025)  # 5.0 * 50 / 10000


def test_estimate_fee_reads_v2_camelcase(fake_clob: MagicMock) -> None:
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    adapter = PolymarketAdapter(
        key_provider=MagicMock(),
        host="http://test",
        chain_id=137,
        clock=lambda: fixed_now,
        client_factory=lambda **_kw: fake_clob,
        default_fee_rate_bps=200,
    )
    fake_clob.get_market.return_value = {"feeRateBps": 100}
    order = Order(
        market_id="0xmarket",
        side="yes",
        size=10.0,
        price=0.50,
        notional_usd=5.0,
        idempotency_key="fee-2",
    )
    fee = adapter.estimate_fee(order)
    assert fee == pytest.approx(0.05)  # 5.0 * 100 / 10000


def test_estimate_fee_falls_back_to_default(fake_clob: MagicMock) -> None:
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    adapter = PolymarketAdapter(
        key_provider=MagicMock(),
        host="http://test",
        chain_id=137,
        clock=lambda: fixed_now,
        client_factory=lambda **_kw: fake_clob,
        default_fee_rate_bps=200,
    )
    fake_clob.get_market.return_value = {}  # No fee field
    order = Order(
        market_id="0xmarket",
        side="yes",
        size=10.0,
        price=0.50,
        notional_usd=100.0,
        idempotency_key="fee-3",
    )
    fee = adapter.estimate_fee(order)
    assert fee == pytest.approx(2.0)  # 100.0 * 200 / 10000


def test_estimate_fee_falls_back_on_network_error(fake_clob: MagicMock) -> None:
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    adapter = PolymarketAdapter(
        key_provider=MagicMock(),
        host="http://test",
        chain_id=137,
        clock=lambda: fixed_now,
        client_factory=lambda **_kw: fake_clob,
        default_fee_rate_bps=200,
    )
    fake_clob.get_market.side_effect = RuntimeError("network down")
    order = Order(
        market_id="0xmarket",
        side="yes",
        size=10.0,
        price=0.50,
        notional_usd=100.0,
        idempotency_key="fee-4",
    )
    fee = adapter.estimate_fee(order)
    assert fee == pytest.approx(2.0)  # falls back to default


def test_to_clob_order_args_maps_side() -> None:
    o = _make_order("k", side="yes")
    args = _to_clob_order_args(o)
    assert args.side == "BUY"
    o2 = _make_order("k2", side="no")
    args2 = _to_clob_order_args(o2)
    assert args2.side == "SELL"


def _make_order(key: str, *, side: Any = "yes") -> Order:
    return Order(
        market_id="0xmarket",
        side=side,
        size=1.0,
        price=0.5,
        notional_usd=0.5,
        idempotency_key=key,
    )
