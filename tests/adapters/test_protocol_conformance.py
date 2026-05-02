"""Cross-adapter conformance tests for `PredictionMarketAdapter`."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from shared.adapters.paper_trading import PaperTradingAdapter
from shared.adapters.polymarket import PolymarketAdapter
from shared.adapters.prediction_market import PredictionMarketAdapter
from tests.adapters.fake_adapter import FakeAdapter


def _build_polymarket() -> PolymarketAdapter:
    fake_clob = MagicMock()
    return PolymarketAdapter(
        key_provider=MagicMock(),
        host="http://test",
        chain_id=137,
        clock=lambda: datetime(2026, 5, 2, tzinfo=UTC),
        client_factory=lambda **_kw: fake_clob,
    )


def _build_paper() -> PaperTradingAdapter:
    @contextmanager
    def factory():
        yield MagicMock()

    return PaperTradingAdapter(
        live_adapter=FakeAdapter(),
        session_factory=factory,
        decision_id_provider=lambda: uuid4(),
        clock=lambda: datetime(2026, 5, 2, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(FakeAdapter, id="fake"),
        pytest.param(_build_polymarket, id="polymarket"),
        pytest.param(_build_paper, id="paper"),
    ],
)
def test_runtime_isinstance(factory: Callable[[], object]) -> None:
    assert isinstance(factory(), PredictionMarketAdapter)


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(FakeAdapter, id="fake"),
        pytest.param(_build_polymarket, id="polymarket"),
        pytest.param(_build_paper, id="paper"),
    ],
)
def test_required_methods_exist(factory: Callable[[], object]) -> None:
    instance = factory()
    for name in ("get_markets", "get_orderbook", "get_metadata", "get_resolution", "place_order", "cancel_order"):
        assert callable(getattr(instance, name)), f"{type(instance).__name__} missing {name}"
