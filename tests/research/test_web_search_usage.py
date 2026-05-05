"""Tests for usage extraction + cost-field persistence in web_search."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from research.skills import web_search as web_search_module
from research.skills.web_search import WebSearchError, WebSearchQuotaError, _extract_usage, web_search
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


def _response(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    n_searches: int = 0,
) -> SimpleNamespace:
    """Build a fake Responses-API response with usage + n web_search_call items."""
    output = [SimpleNamespace(type="web_search_call") for _ in range(n_searches)]
    output.append(SimpleNamespace(type="message", content=[]))
    return SimpleNamespace(
        output_text="ok",
        output=output,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        ),
    )


def test_extract_usage_pulls_all_fields() -> None:
    response = _response(input_tokens=1500, output_tokens=300, cached_tokens=400, n_searches=2)
    usage = _extract_usage(response)
    assert usage.input_tokens == 1500
    assert usage.output_tokens == 300
    assert usage.cached_input_tokens == 400
    assert usage.web_search_count == 2


def test_extract_usage_handles_missing_details() -> None:
    response = SimpleNamespace(
        output=[SimpleNamespace(type="web_search_call")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=20),
    )
    usage = _extract_usage(response)
    assert usage.input_tokens == 10
    assert usage.output_tokens == 20
    assert usage.cached_input_tokens == 0
    assert usage.web_search_count == 1


def test_extract_usage_handles_missing_usage() -> None:
    response = SimpleNamespace(output=[])
    usage = _extract_usage(response)
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cached_input_tokens == 0
    assert usage.web_search_count == 0


def test_persist_receives_usage_and_cost_on_success(
    captured_persists: list[dict[str, Any]],
) -> None:
    response = _response(input_tokens=1000, output_tokens=200, cached_tokens=100, n_searches=3)
    web_search("topic", settings=_settings(), client=_client_returning(response))
    assert len(captured_persists) == 1
    row = captured_persists[0]
    assert row["usage"].input_tokens == 1000
    assert row["usage"].output_tokens == 200
    assert row["usage"].cached_input_tokens == 100
    assert row["usage"].web_search_count == 3
    assert isinstance(row["cost_usd"], Decimal)
    assert row["cost_usd"] > Decimal(0)


def test_cost_is_none_when_pricing_missing(
    monkeypatch: pytest.MonkeyPatch,
    captured_persists: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(Settings, "OPENAI_PRICING", {})
    settings = _settings()
    response = _response(input_tokens=1, output_tokens=1, n_searches=1)
    web_search("topic", settings=settings, client=_client_returning(response))
    row = captured_persists[0]
    assert row["cost_usd"] is None
    assert row["usage"].input_tokens == 1


class _FakeRateLimitError(Exception):
    """Stand-in for openai.RateLimitError."""


def test_quota_failure_persists_zero_usage(
    captured_persists: list[dict[str, Any]],
) -> None:
    _FakeRateLimitError.__name__ = "RateLimitError"
    with pytest.raises(WebSearchQuotaError):
        web_search(
            "topic",
            settings=_settings(),
            client=_client_raising(_FakeRateLimitError("quota")),
        )
    row = captured_persists[0]
    # The persist helper itself decides to NULL these out for failed calls;
    # at the call-site we just need to verify the wrapper passed *something*.
    assert row["error_type"] == "quota_exhausted"
    assert row["usage"].input_tokens == 0
    assert row["cost_usd"] is None


def test_transient_failure_persists_zero_usage(
    captured_persists: list[dict[str, Any]],
) -> None:
    with pytest.raises(WebSearchError):
        web_search("topic", settings=_settings(), client=_client_raising(RuntimeError("boom")))
    row = captured_persists[0]
    assert row["error_type"] == "transient"
    assert row["usage"].input_tokens == 0
    assert row["cost_usd"] is None
