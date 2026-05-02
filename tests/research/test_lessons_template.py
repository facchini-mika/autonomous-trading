"""Determinism + content tests for lesson templates."""

from __future__ import annotations

from uuid import UUID

from research.lessons_template import (
    action_taken,
    hypothesis_for,
    lesson_body,
    stable_lesson_id_seed,
)


def test_lesson_body_is_deterministic() -> None:
    body1 = lesson_body(
        category="missed_yes",
        market_id="0xabc",
        p_yes=0.30,
        outcome_yes=True,
        realized_pnl=-5.0,
        notional=10.0,
        edge=-0.20,
    )
    body2 = lesson_body(
        category="missed_yes",
        market_id="0xabc",
        p_yes=0.30,
        outcome_yes=True,
        realized_pnl=-5.0,
        notional=10.0,
        edge=-0.20,
    )
    assert body1 == body2


def test_lesson_body_includes_all_fields() -> None:
    body = lesson_body(
        category="missed_yes",
        market_id="0xabc",
        p_yes=0.30,
        outcome_yes=True,
        realized_pnl=-5.0,
        notional=10.0,
        edge=-0.20,
        cycle_id="cycle-001",
    )
    assert "missed_yes" in body
    assert "0xabc" in body
    assert "cycle-001" in body
    assert "YES" in body
    assert "p_yes=0.3000" in body


def test_lesson_body_handles_no_pnl() -> None:
    body = lesson_body(
        category="expected",
        market_id="0xabc",
        p_yes=0.5,
        outcome_yes=False,
        realized_pnl=None,
        notional=0.0,
        edge=0.0,
    )
    assert "n/a" in body
    assert "NO" in body


def test_hypothesis_for_known_categories() -> None:
    assert hypothesis_for("missed_yes") is not None
    assert hypothesis_for("missed_no") is not None
    assert hypothesis_for("overconfident_yes") is not None
    assert hypothesis_for("overconfident_no") is not None


def test_hypothesis_for_expected_is_none() -> None:
    assert hypothesis_for("expected") is None


def test_hypothesis_for_unknown_is_none() -> None:
    assert hypothesis_for("invented_category") is None


def test_action_taken_no_pnl() -> None:
    assert action_taken(None) == "no trade was placed"


def test_action_taken_positive_pnl() -> None:
    assert "positive" in action_taken(5.50)
    assert "+5.50" in action_taken(5.50)


def test_action_taken_negative_pnl() -> None:
    assert "negative" in action_taken(-3.25)
    assert "-3.25" in action_taken(-3.25)


def test_stable_lesson_id_seed() -> None:
    uid = UUID("11111111-1111-4111-8111-111111111111")
    seed = stable_lesson_id_seed(source_agent_id="trading-agent", trigger_event_id=uid)
    assert "trading-agent" in seed
    assert str(uid) in seed
