"""Round-trip tests for Pydantic models — JSON serialize + deserialize equality."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from shared.models import (
    CashBalance,
    Decision,
    GateResult,
    Market,
    Order,
    Orderbook,
    PortfolioState,
    Position,
    Prediction,
    Trade,
)


def test_market_round_trip() -> None:
    now = datetime.now(UTC)
    m = Market(
        market_id="m1",
        condition_id="0x" + "ab" * 32,
        slug="trump-2028",
        title="Will Trump run in 2028?",
        category="politics",
        end_date=now,
        status="open",
        created_at=now,
        last_seen=now,
    )
    assert Market.model_validate_json(m.model_dump_json()) == m


def test_orderbook_round_trip() -> None:
    o = Orderbook(
        market_id="m1",
        best_bid=0.42,
        best_ask=0.45,
        mid=0.435,
        depth_bid_1pct=100.0,
        depth_ask_1pct=120.0,
        timestamp=datetime.now(UTC),
    )
    assert Orderbook.model_validate_json(o.model_dump_json()) == o


def test_portfolio_state_round_trip() -> None:
    now = datetime.now(UTC)
    cash = CashBalance(total_usd=1000.0, available=900.0, reserved_for_orders=100.0, timestamp=now)
    pos = Position(
        market_id="m1",
        side="yes",
        size=50.0,
        avg_price=0.5,
        unrealized_pnl=10.0,
        realized_pnl=0.0,
        opened_at=now,
        last_updated=now,
    )
    state = PortfolioState(
        cash=cash,
        positions=[pos],
        gross_exposure_usd=25.0,
        unrealized_pnl=10.0,
        realized_pnl=0.0,
        equity=1010.0,
        timestamp=now,
    )
    assert PortfolioState.model_validate_json(state.model_dump_json()) == state


def test_order_round_trip() -> None:
    o = Order(
        market_id="m1",
        side="yes",
        size=10.0,
        price=0.42,
        notional_usd=4.2,
        idempotency_key="cycle-1-trade-1",
    )
    assert Order.model_validate_json(o.model_dump_json()) == o


def test_prediction_round_trip() -> None:
    p = Prediction(
        market_id="m1",
        agent_id="trading-agent",
        p_yes=0.55,
        reasoning="reasons here",
        edge=0.05,
        latency_ms=1234,
        created_at=datetime.now(UTC),
    )
    assert Prediction.model_validate_json(p.model_dump_json()) == p


def _make_prediction(**overrides: object) -> Prediction:
    base: dict[str, object] = {
        "market_id": "m1",
        "agent_id": "trading-agent",
        "p_yes": 0.55,
        "reasoning": "reasons",
        "edge": 0.05,
        "latency_ms": 100,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Prediction(**base)


def test_prediction_infers_web_search_called_true_from_sources() -> None:
    p = _make_prediction(inference_log={"sources": ["http://x"]})
    assert p.inference_log["web_search_called"] is True


def test_prediction_infers_web_search_called_false_from_empty_sources() -> None:
    p = _make_prediction(inference_log={"sources": []})
    assert p.inference_log["web_search_called"] is False


def test_prediction_infers_web_search_called_false_when_no_sources_key() -> None:
    p = _make_prediction(inference_log={"thesis": "x"})
    assert p.inference_log["web_search_called"] is False


def test_prediction_preserves_explicit_web_search_called_true() -> None:
    p = _make_prediction(inference_log={"web_search_called": True, "sources": []})
    assert p.inference_log["web_search_called"] is True


def test_prediction_preserves_explicit_web_search_called_false() -> None:
    p = _make_prediction(
        inference_log={"web_search_called": False, "sources": ["http://x"]},
    )
    assert p.inference_log["web_search_called"] is False


def test_decision_round_trip() -> None:
    d = Decision(
        cycle_id="cycle-2026-05-02-12-00",
        market_id="m1",
        p_consensus=0.55,
        q_market=0.50,
        edge=0.05,
        action="trade",
        rationale="edge above threshold",
        created_at=datetime.now(UTC),
    )
    assert Decision.model_validate_json(d.model_dump_json()) == d


def test_trade_round_trip() -> None:
    t = Trade(
        decision_id=uuid4(),
        market_id="m1",
        side="yes",
        size=10.0,
        price=0.42,
        notional_usd=4.2,
        status="filled",
        created_at=datetime.now(UTC),
    )
    assert Trade.model_validate_json(t.model_dump_json()) == t


def test_gate_result_round_trip() -> None:
    g = GateResult(
        gate_name="concentration",
        passed=False,
        reason="exceeds 15% cap",
        clipped_notional=150.0,
    )
    assert GateResult.model_validate_json(g.model_dump_json()) == g


def test_models_are_frozen() -> None:
    o = Order(
        market_id="m1",
        side="yes",
        size=10.0,
        price=0.42,
        notional_usd=4.2,
        idempotency_key="k",
    )
    try:
        o.size = 100.0  # type: ignore[misc]
    except (TypeError, ValueError):
        return
    raise AssertionError("Order should be frozen but accepted mutation")
