"""Tests for reconciliation (Gamma-implied vs internal PnL)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from execution.reconcile import reconcile_market
from shared.config.settings import Settings


def _row(**kwargs):
    return SimpleNamespace(**kwargs)


def test_no_flag_when_diff_below_threshold() -> None:
    sess = MagicMock()
    selected = MagicMock()
    # YES @ 0.5 size 10 → expected pnl on YES win = 10*(1-0.5)-0=5; actual 5.10 → diff 0.10 < 0.50
    selected.all.return_value = [
        _row(id="t1", side="yes", size=10.0, price=0.5, fees=0.0, gas=0.0, realized_pnl=5.10),
    ]
    sess.execute.return_value = selected

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    flagged = reconcile_market(
        "0xabc",
        outcome_yes=True,
        settings=Settings(),
        session_factory=factory,
    )
    assert flagged == 0


def test_flag_when_diff_above_threshold() -> None:
    sess = MagicMock()
    selected = MagicMock()
    # Expected = 5.0; actual 6.0 → diff 1.0 > 0.50
    selected.all.return_value = [
        _row(id="t1", side="yes", size=10.0, price=0.5, fees=0.0, gas=0.0, realized_pnl=6.0),
    ]
    update_result = MagicMock()
    sess.execute.side_effect = [selected, update_result]

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    flagged = reconcile_market("0xabc", outcome_yes=True, session_factory=factory)
    assert flagged == 1
    insert_call = sess.execute.call_args_list[1]
    sql = str(insert_call.args[0])
    assert "system_state" in sql
    params = insert_call.args[1]
    assert params["k"] == "reconciliation_flag:t1"


def test_threshold_boundary_does_not_flag() -> None:
    sess = MagicMock()
    selected = MagicMock()
    # diff exactly threshold (0.50) — strict > comparison → should NOT flag
    selected.all.return_value = [
        _row(id="t1", side="yes", size=10.0, price=0.5, fees=0.0, gas=0.0, realized_pnl=5.5),
    ]
    sess.execute.return_value = selected

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    flagged = reconcile_market("0xabc", outcome_yes=True, session_factory=factory)
    assert flagged == 0


def test_returns_zero_when_no_trades() -> None:
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = []
    sess.execute.return_value = selected

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    assert reconcile_market("0xabc", outcome_yes=True, session_factory=factory) == 0
