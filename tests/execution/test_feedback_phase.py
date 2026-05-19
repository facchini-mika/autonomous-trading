"""Tests for the inline feedback phase (run before the trading-agent)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from execution.feedback_phase import run_feedback_phase
from shared.config.settings import Settings


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "INLINE_FEEDBACK_ENABLED": True,
        "INLINE_OUTCOME_INGESTION_ENABLED": True,
        "INLINE_LESSONS_SUMMARY_ENABLED": True,
        "INLINE_TIER1_EVAL_ENABLED": False,
        "INLINE_TIER1_EVAL_INTERVAL_MIN": 60,
        "INLINE_FEEDBACK_TIMEOUT_SEC": 5,
        "INLINE_OUTCOME_LOOKBACK_DAYS": 1,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def test_master_flag_off_skips_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def fail(*_a: object, **_kw: object) -> None:
        called.append("called")

    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", fail)
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", fail)

    result = run_feedback_phase(
        settings=_settings(INLINE_FEEDBACK_ENABLED=False),
        cycle_id="cycle-test",
        now=datetime.now(UTC),
    )
    assert result.skipped is True
    assert called == []


def test_runs_outcome_before_lessons(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    def fake_outcome(**_kwargs: object) -> dict[str, int]:
        order.append("outcome")
        return {"predictions": 2}

    def fake_lessons(**_kwargs: object) -> int:
        order.append("lessons")
        return 3

    gamma = MagicMock()
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", fake_outcome)
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", fake_lessons)

    result = run_feedback_phase(
        settings=_settings(),
        cycle_id="c1",
        now=datetime.now(UTC),
        gamma_factory=lambda: gamma,
    )
    assert order == ["outcome", "lessons"]
    assert result.outcome_counts == {"predictions": 2}
    assert result.lessons_inserted == 3
    assert result.outcome_error is None
    assert result.lessons_error is None


def test_continues_when_outcome_ingestion_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_outcome(**_kwargs: object) -> dict[str, int]:
        msg = "gamma 503"
        raise RuntimeError(msg)

    def fake_lessons(**_kwargs: object) -> int:
        return 1

    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", raising_outcome)
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", fake_lessons)

    result = run_feedback_phase(
        settings=_settings(),
        cycle_id="c2",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
    )
    assert result.outcome_error is not None
    assert "gamma 503" in result.outcome_error
    assert result.lessons_inserted == 1


def test_continues_when_lessons_summary_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_outcome(**_kwargs: object) -> dict[str, int]:
        return {}

    def raising_lessons(**_kwargs: object) -> int:
        msg = "db error"
        raise RuntimeError(msg)

    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", fake_outcome)
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", raising_lessons)

    result = run_feedback_phase(
        settings=_settings(),
        cycle_id="c3",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
    )
    assert result.outcome_error is None
    assert result.lessons_error is not None
    assert "db error" in result.lessons_error


def test_individual_flags_disable_individual_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def fake_outcome(**_kwargs: object) -> dict[str, int]:
        called.append("outcome")
        return {}

    def fake_lessons(**_kwargs: object) -> int:
        called.append("lessons")
        return 0

    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", fake_outcome)
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", fake_lessons)

    run_feedback_phase(
        settings=_settings(INLINE_OUTCOME_INGESTION_ENABLED=False),
        cycle_id="c4",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
    )
    assert called == ["lessons"]

    called.clear()
    run_feedback_phase(
        settings=_settings(INLINE_LESSONS_SUMMARY_ENABLED=False),
        cycle_id="c5",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
    )
    assert called == ["outcome"]


def test_timeout_kills_slow_step(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow_outcome(**_kwargs: object) -> dict[str, int]:
        time.sleep(2.0)
        return {}

    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", slow_outcome)
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    result = run_feedback_phase(
        settings=_settings(INLINE_FEEDBACK_TIMEOUT_SEC=1),
        cycle_id="c6",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
    )
    assert result.outcome_error is not None
    assert "exceeded" in result.outcome_error


def test_clock_is_propagated_to_substeps(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, datetime] = {}

    def fake_outcome(**kwargs: object) -> dict[str, int]:
        clock = kwargs["clock"]
        captured["outcome"] = clock()  # type: ignore[operator]
        return {}

    def fake_lessons(**kwargs: object) -> int:
        clock = kwargs["clock"]
        captured["lessons"] = clock()  # type: ignore[operator]
        return 0

    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", fake_outcome)
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", fake_lessons)

    fixed = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    run_feedback_phase(
        settings=_settings(),
        cycle_id="c7",
        now=fixed,
        gamma_factory=lambda: MagicMock(),
    )
    assert captured["outcome"] == fixed
    assert captured["lessons"] == fixed


def test_owned_gamma_client_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    gamma = MagicMock()
    monkeypatch.setattr("execution.feedback_phase._default_gamma_factory", lambda: gamma)
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", lambda **_k: {})
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    run_feedback_phase(
        settings=_settings(),
        cycle_id="c8",
        now=datetime.now(UTC),
    )
    gamma.close.assert_called_once()


def test_injected_gamma_client_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    gamma = MagicMock()
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", lambda **_k: {})
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    run_feedback_phase(
        settings=_settings(),
        cycle_id="c9",
        now=datetime.now(UTC),
        gamma_factory=lambda: gamma,
    )
    gamma.close.assert_not_called()


def test_tier1_eval_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    eval_calls: list[int] = []
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", lambda **_k: {})
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    def fake_runner() -> int:
        eval_calls.append(1)
        return 5

    run_feedback_phase(
        settings=_settings(INLINE_TIER1_EVAL_ENABLED=False),
        cycle_id="c10",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
        tier1_runner=fake_runner,
    )
    assert eval_calls == []


def test_tier1_eval_gating_skips_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    eval_calls: list[int] = []
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", lambda **_k: {})
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    recent = now - timedelta(minutes=10)

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sess.execute.return_value.first.return_value = SimpleNamespace(
            value={"at": recent.isoformat()},
        )
        yield sess

    def fake_runner() -> int:
        eval_calls.append(1)
        return 0

    run_feedback_phase(
        settings=_settings(INLINE_TIER1_EVAL_ENABLED=True),
        cycle_id="c11",
        now=now,
        gamma_factory=lambda: MagicMock(),
        eval_session_factory=factory,
        tier1_runner=fake_runner,
    )
    assert eval_calls == []


def test_tier1_eval_runs_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    eval_calls: list[int] = []
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", lambda **_k: {})
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    now = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    stale = now - timedelta(hours=2)

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sess.execute.return_value.first.return_value = SimpleNamespace(
            value={"at": stale.isoformat()},
        )
        yield sess

    def fake_runner() -> int:
        eval_calls.append(1)
        return 7

    result = run_feedback_phase(
        settings=_settings(INLINE_TIER1_EVAL_ENABLED=True),
        cycle_id="c12",
        now=now,
        gamma_factory=lambda: MagicMock(),
        eval_session_factory=factory,
        tier1_runner=fake_runner,
    )
    assert eval_calls == [1]
    assert result.tier1_eval_ran is True
    assert result.tier1_eval_rows == 7


def test_tier1_eval_runs_when_hwm_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    eval_calls: list[int] = []
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", lambda **_k: {})
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sess.execute.return_value.first.return_value = None
        yield sess

    def fake_runner() -> int:
        eval_calls.append(1)
        return 0

    run_feedback_phase(
        settings=_settings(INLINE_TIER1_EVAL_ENABLED=True),
        cycle_id="c13",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
        eval_session_factory=factory,
        tier1_runner=fake_runner,
    )
    assert eval_calls == [1]


def test_tier1_eval_failure_does_not_break_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("execution.feedback_phase.outcome_ingestion.run_once", lambda **_k: {})
    monkeypatch.setattr("execution.feedback_phase.lessons_summary.run_once", lambda **_k: 0)

    @contextmanager
    def factory() -> Iterator[MagicMock]:
        sess = MagicMock()
        sess.execute.return_value.first.return_value = None
        yield sess

    def boom() -> int:
        msg = "subagent crashed"
        raise RuntimeError(msg)

    result = run_feedback_phase(
        settings=_settings(INLINE_TIER1_EVAL_ENABLED=True),
        cycle_id="c14",
        now=datetime.now(UTC),
        gamma_factory=lambda: MagicMock(),
        eval_session_factory=factory,
        tier1_runner=boom,
    )
    assert result.tier1_eval_ran is False
    assert result.tier1_eval_error is not None
    assert "subagent crashed" in result.tier1_eval_error
