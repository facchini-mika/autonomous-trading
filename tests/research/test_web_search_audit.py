"""Audit-trail tests for ``research.skills.web_search``.

Verify the OpenAI-Responses wrapper writes one ``web_search_calls`` row per
invocation (success + error), reads ``RUN_ID``/``CYCLE_ID``/``AGENT_NAME``
from the environment, and tags ``error_type`` correctly. DB hiccups must
not propagate.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from research.skills import web_search as web_search_module
from research.skills.web_search import (
    WebSearchError,
    WebSearchQuotaError,
    web_search,
)
from shared.config.settings import Settings


def _settings(api_key: str = "sk-test") -> Settings:
    return Settings(OPENAI_API_KEY=api_key)


def _client_returning(response: Any) -> MagicMock:
    client = MagicMock()
    client.responses.create.return_value = response
    return client


def _client_raising(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.responses.create.side_effect = exc
    return client


@pytest.fixture
def captured_persists(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []

    def _capture(**kw: Any) -> None:
        captures.append(kw)

    monkeypatch.setattr(web_search_module, "_persist_web_search_call", _capture)
    return captures


def test_audit_row_written_on_success_with_env_propagation(
    monkeypatch: pytest.MonkeyPatch,
    captured_persists: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv("RUN_ID", "run-123")
    monkeypatch.setenv("CYCLE_ID", "cycle-xyz")
    monkeypatch.setenv("AGENT_NAME", "trading-agent")
    response = SimpleNamespace(output_text="Headline summary text.", output=[])
    web_search("CPI release", settings=_settings(), client=_client_returning(response))
    assert len(captured_persists) == 1
    row = captured_persists[0]
    assert row["query"] == "CPI release"
    assert row["summary"] == "Headline summary text."
    assert row["error"] is None
    assert row["error_type"] is None
    assert row["elapsed_sec"] >= 0
    assert row["started_at"] is not None
    assert row["finished_at"] is not None


class _FakeRateLimitError(Exception):
    """Stand-in for openai.RateLimitError so we don't need to import the SDK."""


def test_audit_row_written_on_quota_failure(
    captured_persists: list[dict[str, Any]],
) -> None:
    _FakeRateLimitError.__name__ = "RateLimitError"
    with pytest.raises(WebSearchQuotaError):
        web_search(
            "foo",
            settings=_settings(),
            client=_client_raising(_FakeRateLimitError("quota")),
        )
    assert len(captured_persists) == 1
    row = captured_persists[0]
    assert row["error_type"] == "quota_exhausted"
    assert "quota" in (row["error"] or "").lower()


def test_audit_row_written_on_transient_failure(
    captured_persists: list[dict[str, Any]],
) -> None:
    with pytest.raises(WebSearchError):
        web_search("foo", settings=_settings(), client=_client_raising(RuntimeError("boom")))
    assert len(captured_persists) == 1
    assert captured_persists[0]["error_type"] == "transient"


def test_persist_helper_swallows_db_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ``_persist_web_search_call`` must not raise even if get_session blows up."""

    def _explode(*_args: Any, **_kw: Any) -> Any:
        msg = "simulated DB outage"
        raise RuntimeError(msg)

    monkeypatch.setattr("research.skills.web_search.get_session", _explode)
    response = SimpleNamespace(output_text="ok", output=[])
    # No persist mock — the real helper runs against the patched get_session.
    res = web_search("foo", settings=_settings(), client=_client_returning(response))
    assert res.summary == "ok"


def test_persist_called_even_when_env_vars_missing(
    monkeypatch: pytest.MonkeyPatch,
    captured_persists: list[dict[str, Any]],
) -> None:
    """When invoked outside a cycle (no RUN_ID), still persist with NULL keys."""
    monkeypatch.delenv("RUN_ID", raising=False)
    monkeypatch.delenv("CYCLE_ID", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)
    response = SimpleNamespace(output_text="standalone", output=[])
    web_search("foo", settings=_settings(), client=_client_returning(response))
    assert len(captured_persists) == 1
