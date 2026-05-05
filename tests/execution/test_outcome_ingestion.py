"""Tests for outcome_ingestion (mocked Gamma + Session)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from execution.outcome_ingestion import (
    HIGH_WATER_MARK_KEY,
    _read_high_water_mark,
    _set_predictions_outcome,
    _set_predictions_realized_pnl,
    _set_trades_pnl,
    run_once,
)


def _row(**kwargs):
    return SimpleNamespace(**kwargs)


def test_set_predictions_outcome_updates_only_null() -> None:
    sess = MagicMock()
    sess.execute.return_value.rowcount = 3
    n = _set_predictions_outcome(sess, "0xabc", outcome_yes=True)
    assert n == 3
    sql = str(sess.execute.call_args.args[0])
    assert "outcome IS NULL" in sql
    # outcome and realized_pnl are written in separate UPDATEs: this one
    # only sets outcome; realized_pnl is aggregated downstream after trades
    # have been priced.
    assert "realized_pnl" not in sql


def test_set_predictions_realized_pnl_aggregates_via_cycle_id() -> None:
    sess = MagicMock()
    sess.execute.return_value.rowcount = 2
    n = _set_predictions_realized_pnl(sess, "0xabc")
    assert n == 2
    sql = str(sess.execute.call_args.args[0])
    # Aggregation joins predictions to decisions on (cycle_id, market_id),
    # then sums trades + paper_trades realized_pnl, COALESCE-ing 0-trade
    # predictions to 0.0 instead of NULL.
    assert "UPDATE predictions" in sql
    assert "cycle_id = p2.cycle_id" in sql
    assert "trades" in sql
    assert "paper_trades" in sql
    assert "COALESCE" in sql.upper()
    assert "realized_pnl IS NULL" in sql
    assert "p2.cycle_id IS NOT NULL" in sql


def test_set_predictions_realized_pnl_idempotent_with_zero_rows() -> None:
    sess = MagicMock()
    sess.execute.return_value.rowcount = 0
    n = _set_predictions_realized_pnl(sess, "0xabc")
    assert n == 0


def test_set_trades_pnl_writes_one_update_per_filled_trade() -> None:
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = [
        _row(id="t1", side="yes", size=10.0, price=0.5, fees=0.1, gas=0.0),
        _row(id="t2", side="no", size=5.0, price=0.6, fees=0.05, gas=0.0),
    ]
    update_result = MagicMock()
    sess.execute.side_effect = [selected, update_result, update_result]
    n = _set_trades_pnl(sess, "0xabc", outcome_yes=True, table="paper_trades")
    assert n == 2
    update_calls = [c for c in sess.execute.call_args_list if "UPDATE" in str(c.args[0]).upper()]
    assert len(update_calls) == 2


def test_run_once_iterates_and_writes_high_water_mark() -> None:
    gamma = MagicMock()
    raw_market = {"condition_id": "0xa"}
    gamma.list_resolved_markets.return_value = iter([raw_market])
    gamma.get_resolution.return_value = SimpleNamespace(
        market_id="0xa",
        outcome=True,
        resolved_at=datetime.now(UTC),
        settlement_price=1.0,
        disputed=False,
    )

    sessions: list[MagicMock] = []

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sessions.append(sess)
        sess.execute.return_value.first.return_value = None
        sess.execute.return_value.all.return_value = []
        sess.execute.return_value.rowcount = 0
        yield sess

    counts = run_once(gamma=gamma, session_factory=factory, lookback_days=7)
    assert counts == {
        "predictions": 0,
        "trades": 0,
        "paper_trades": 0,
        "positions": 0,
        "predictions_pnl": 0,
    }
    last_sql = str(sessions[-1].execute.call_args.args[0])
    assert "system_state" in last_sql.lower()
    assert "INSERT" in last_sql.upper()


def test_run_once_skips_market_without_id() -> None:
    gamma = MagicMock()
    gamma.list_resolved_markets.return_value = iter([{"foo": "bar"}])

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sess.execute.return_value.first.return_value = None
        sess.execute.return_value.all.return_value = []
        sess.execute.return_value.rowcount = 0
        yield sess

    counts = run_once(gamma=gamma, session_factory=factory)
    assert counts["predictions"] == 0
    gamma.get_resolution.assert_not_called()


def test_high_water_mark_falls_back_when_missing() -> None:
    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sess.execute.return_value.first.return_value = None
        yield sess

    fallback = datetime(2020, 1, 1, tzinfo=UTC)
    out = _read_high_water_mark(factory, fallback=fallback)
    assert out == fallback


def test_high_water_mark_parses_jsonb_value() -> None:
    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sess.execute.return_value.first.return_value = ({"ts": "2026-04-01T00:00:00Z"},)
        yield sess

    fallback = datetime(2020, 1, 1, tzinfo=UTC)
    out = _read_high_water_mark(factory, fallback=fallback)
    assert out.year == 2026
    assert out.month == 4


def test_high_water_mark_constant() -> None:
    assert HIGH_WATER_MARK_KEY == "last_outcome_ingestion_at"
