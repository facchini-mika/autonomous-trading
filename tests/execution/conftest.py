"""Shared fixtures for execution tests.

By default, ``run_subagent`` writes a row to ``subagent_runs`` after every
invocation (best-effort). Unit tests mock the subprocess, not the database,
so the persist would either fail (no DB) or pollute a real one. Disable it
by default and let the dedicated audit-trail tests opt back in by patching
``execution.subagent_runner._persist_subagent_run`` themselves.
"""

from __future__ import annotations

import pytest

from execution import feedback_phase


@pytest.fixture(autouse=True)
def _no_subagent_audit_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "execution.subagent_runner._persist_subagent_run",
        lambda **_kw: None,
    )


@pytest.fixture(autouse=True)
def _stub_inline_feedback_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """No-op the inline feedback phase by default.

    The Lead invokes ``run_feedback_phase`` at the top of every cycle, which
    would otherwise reach for Gamma + the outcome_ingestion + lessons_summary
    Postgres roles. Tests that need to assert on feedback behaviour patch
    this attribute back to the real implementation themselves.
    """
    monkeypatch.setattr(
        "execution.lead_bootstrap.run_feedback_phase",
        lambda **_kw: feedback_phase.FeedbackPhaseResult(cycle_id="stubbed"),
    )
