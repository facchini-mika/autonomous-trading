"""Tests for the OpenAI web_search skill."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from research.skills.web_search import (
    WebSearchError,
    WebSearchHit,
    WebSearchResult,
    web_search,
)
from shared.config.settings import Settings


def _settings(api_key: str = "sk-test") -> Settings:
    return Settings(OPENAI_API_KEY=api_key)


def _mock_client(response: Any) -> MagicMock:
    client = MagicMock()
    client.responses.create.return_value = response
    return client


def test_returns_pydantic_result() -> None:
    response = SimpleNamespace(
        output_text="Recent CPI rose to 3.2%. https://example.com/cpi",
        output=[],
    )
    client = _mock_client(response)
    res = web_search("CPI", settings=_settings(), client=client)
    assert isinstance(res, WebSearchResult)
    assert res.query == "CPI"
    assert "CPI rose" in res.summary
    assert res.elapsed_sec >= 0
    assert res.model_used == _settings().OPENAI_MODEL


def test_extracts_hits_from_web_search_call() -> None:
    response = SimpleNamespace(
        output_text="see https://x.test for details",
        output=[
            SimpleNamespace(
                type="web_search_call",
                results=[
                    SimpleNamespace(url="https://x.test", title="X", snippet="X test"),
                    SimpleNamespace(url="https://y.test", title="Y", snippet="Y test"),
                ],
            ),
        ],
    )
    res = web_search("q", settings=_settings(), client=_mock_client(response))
    urls = {h.url for h in res.hits}
    assert urls == {"https://x.test", "https://y.test"}
    cited = next(h for h in res.hits if h.url == "https://x.test")
    assert cited.cited_in_summary is True


def test_handles_no_hits() -> None:
    response = SimpleNamespace(output_text="no results", output=[])
    res = web_search("q", settings=_settings(), client=_mock_client(response))
    assert res.hits == []


def test_passes_query_to_openai() -> None:
    response = SimpleNamespace(output_text="ok", output=[])
    client = _mock_client(response)
    web_search("specific query", settings=_settings(), client=client)
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["input"] == "specific query"
    assert kwargs["tools"] == [{"type": "web_search_preview"}]


def test_passes_timeout() -> None:
    response = SimpleNamespace(output_text="ok", output=[])
    client = _mock_client(response)
    web_search("q", settings=_settings(), client=client)
    assert client.responses.create.call_args.kwargs["timeout"] == 60.0


def test_missing_api_key_raises() -> None:
    with pytest.raises(WebSearchError, match="OPENAI_API_KEY"):
        web_search("q", settings=_settings(api_key=""))


def test_openai_exception_wrapped_as_websearch_error() -> None:
    client = MagicMock()
    client.responses.create.side_effect = RuntimeError("network down")
    with pytest.raises(WebSearchError, match="failed"):
        web_search("q", settings=_settings(), client=client)


def test_websearchhit_is_frozen() -> None:
    hit = WebSearchHit(url="u", title="t", snippet="s")
    with pytest.raises((AttributeError, ValueError)):
        hit.url = "other"  # type: ignore[misc]
