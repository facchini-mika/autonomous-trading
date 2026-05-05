"""Tests for the lessons_summary script (mocked Session)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError

from research.skills.lessons_summary import HIGH_WATER_MARK_KEY, run_once
from shared.config.settings import Settings


def _row(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


def _surprising_prediction() -> SimpleNamespace:
    return _row(
        id="11111111-1111-4111-8111-111111111111",
        market_id="0xabc",
        agent_id="trading-agent",
        p_raw=0.10,
        outcome=True,
        realized_pnl=-5.0,
        created_at=datetime.now(UTC),
        cycle_id="cycle-1",
        edge=-0.30,
        action="trade",
        notional_usd=10.0,
    )


def _expected_prediction() -> SimpleNamespace:
    return _row(
        id="22222222-2222-4222-8222-222222222222",
        market_id="0xdef",
        agent_id="trading-agent",
        p_raw=0.95,
        outcome=True,
        realized_pnl=4.0,
        created_at=datetime.now(UTC),
        cycle_id="cycle-1",
        edge=0.45,
        action="trade",
        notional_usd=10.0,
    )


def test_inserts_lesson_for_surprising_prediction() -> None:
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = [_surprising_prediction()]
    sess.execute.return_value = selected

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    n = run_once(settings=Settings(), session_factory=factory)
    assert n == 1
    insert_calls = [c for c in sess.execute.call_args_list if "INSERT INTO lessons" in str(c.args[0])]
    assert len(insert_calls) == 1
    params = insert_calls[0].args[1]
    assert params["agent_id"] == "trading-agent"
    assert params["market_id"] == "0xabc"


def test_skips_expected_prediction() -> None:
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = [_expected_prediction()]
    sess.execute.return_value = selected

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    n = run_once(settings=Settings(), session_factory=factory)
    assert n == 0


def test_idempotent_re_run_swallows_unique_violation() -> None:
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = [_surprising_prediction()]

    def execute(stmt: Any, params: Any = None) -> Any:
        sql = str(stmt)
        if "INSERT INTO lessons" in sql:
            raise IntegrityError("duplicate", params, Exception())
        if "SELECT" in sql.upper() and "predictions" in sql:
            return selected
        return MagicMock()

    sess.execute.side_effect = execute

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    n = run_once(settings=Settings(), session_factory=factory)
    assert n == 0


def test_high_water_mark_written() -> None:
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = []
    sess.execute.return_value = selected

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    run_once(settings=Settings(), session_factory=factory)
    insert_state_calls = [c for c in sess.execute.call_args_list if HIGH_WATER_MARK_KEY in str(c.args[1] or {})]
    assert len(insert_state_calls) == 1


def test_join_uses_cycle_id_to_disambiguate_multi_cycle_markets() -> None:
    # The pre-fix LEFT JOIN matched predictions to decisions on market_id
    # alone, fanning out across every cycle that touched the same market.
    # Migration 0005 + this query change tighten the join to (cycle_id,
    # market_id), so each prediction picks up exactly its own cycle's
    # decision/trade.
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = []
    sess.execute.return_value = selected

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        yield sess

    run_once(settings=Settings(), session_factory=factory)
    select_calls = [c for c in sess.execute.call_args_list if "FROM predictions p" in str(c.args[0])]
    assert len(select_calls) == 1
    sql = str(select_calls[0].args[0])
    assert "d.cycle_id = p.cycle_id" in sql


def test_constants() -> None:
    assert HIGH_WATER_MARK_KEY == "last_lessons_summary_at"
