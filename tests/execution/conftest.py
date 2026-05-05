"""Shared fixtures for execution tests.

By default, ``run_subagent`` writes a row to ``subagent_runs`` after every
invocation (best-effort). Unit tests mock the subprocess, not the database,
so the persist would either fail (no DB) or pollute a real one. Disable it
by default and let the dedicated audit-trail tests opt back in by patching
``execution.subagent_runner._persist_subagent_run`` themselves.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_subagent_audit_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "execution.subagent_runner._persist_subagent_run",
        lambda **_kw: None,
    )
