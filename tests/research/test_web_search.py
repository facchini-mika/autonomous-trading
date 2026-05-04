"""Tests for the OpenAI web_search skill."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from research.skills.web_search import (
    WebSearchError,
    WebSearchHit,
    WebSearchQuotaError,
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


def _ga_response(summary: str, citations: list[dict[str, str]]) -> SimpleNamespace:
    """Build a GA `web_search` response shape with `message.content[*].annotations`."""
    annotations = [
        SimpleNamespace(
            type="url_citation",
            url=c["url"],
            title=c.get("title", c["url"]),
            start_index=c.get("start_index", 0),
            end_index=c.get("end_index", len(summary)),
        )
        for c in citations
    ]
    message = SimpleNamespace(
        type="message",
        content=[SimpleNamespace(text=summary, annotations=annotations)],
    )
    return SimpleNamespace(
        output_text=summary,
        output=[
            SimpleNamespace(type="reasoning"),
            SimpleNamespace(type="web_search_call", action=None, status="completed", id="ws_x"),
            message,
        ],
    )


def test_extracts_hits_from_message_annotations() -> None:
    response = _ga_response(
        "Story summary citing two sources.",
        [
            {"url": "https://x.test", "title": "X article"},
            {"url": "https://y.test", "title": "Y article"},
        ],
    )
    res = web_search("q", settings=_settings(), client=_mock_client(response))
    urls = {h.url for h in res.hits}
    assert urls == {"https://x.test", "https://y.test"}
    titles = {h.title for h in res.hits}
    assert titles == {"X article", "Y article"}
    # GA annotations are citations by construction → always cited.
    assert all(h.cited_in_summary for h in res.hits)
    # GA does not surface a snippet field on annotations.
    assert all(h.snippet == "" for h in res.hits)


def test_extracts_hits_deduplicates_repeated_urls() -> None:
    response = _ga_response(
        "Same source cited twice.",
        [
            {"url": "https://x.test", "title": "X"},
            {"url": "https://x.test", "title": "X (again)"},
        ],
    )
    res = web_search("q", settings=_settings(), client=_mock_client(response))
    assert [h.url for h in res.hits] == ["https://x.test"]


def test_extracts_hits_skips_blocked_domains() -> None:
    response = _ga_response(
        "CMC and Bloomberg cited.",
        [
            {"url": "https://pro.coinmarketcap.com/btc", "title": "CMC"},
            {"url": "https://www.bloomberg.com/btc", "title": "Bloomberg"},
        ],
    )
    settings = Settings(OPENAI_API_KEY="sk-test", WEB_SEARCH_BLOCKED_DOMAINS=["coinmarketcap.com"])
    res = web_search("q", settings=settings, client=_mock_client(response))
    assert [h.url for h in res.hits] == ["https://www.bloomberg.com/btc"]


def test_extracts_hits_ignores_non_url_citation_annotations() -> None:
    response = SimpleNamespace(
        output_text="text",
        output=[
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        text="text",
                        annotations=[
                            SimpleNamespace(type="file_citation", url="ignored://x"),
                            SimpleNamespace(type="url_citation", url="https://kept.test", title="Kept"),
                        ],
                    ),
                ],
            ),
        ],
    )
    res = web_search("q", settings=_settings(), client=_mock_client(response))
    assert [h.url for h in res.hits] == ["https://kept.test"]


def test_handles_no_hits() -> None:
    response = SimpleNamespace(output_text="no results", output=[])
    res = web_search("q", settings=_settings(), client=_mock_client(response))
    assert res.hits == []


def test_handles_message_without_annotations() -> None:
    response = SimpleNamespace(
        output_text="model answered without citing",
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(text="model answered without citing", annotations=None)],
            ),
        ],
    )
    res = web_search("q", settings=_settings(), client=_mock_client(response))
    assert res.hits == []


def test_passes_query_to_openai() -> None:
    response = SimpleNamespace(output_text="ok", output=[])
    client = _mock_client(response)
    web_search("specific query", settings=_settings(), client=client)
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["input"] == "specific query"
    assert kwargs["tools"] == [{"type": "web_search"}]


def test_passes_timeout() -> None:
    response = SimpleNamespace(output_text="ok", output=[])
    client = _mock_client(response)
    web_search("q", settings=_settings(), client=client)
    assert client.responses.create.call_args.kwargs["timeout"] == 120.0


def test_missing_api_key_raises() -> None:
    with pytest.raises(WebSearchError, match="OPENAI_API_KEY"):
        web_search("q", settings=_settings(api_key=""))


def test_openai_exception_wrapped_as_websearch_error() -> None:
    client = MagicMock()
    client.responses.create.side_effect = RuntimeError("network down")
    with pytest.raises(WebSearchError, match="failed"):
        web_search("q", settings=_settings(), client=client)


def test_rate_limit_error_class_maps_to_quota_error() -> None:
    class RateLimitError(Exception):
        pass

    client = MagicMock()
    client.responses.create.side_effect = RateLimitError("Rate limit exceeded")
    with pytest.raises(WebSearchQuotaError, match="quota/rate-limit"):
        web_search("q", settings=_settings(), client=client)


def test_insufficient_quota_message_maps_to_quota_error() -> None:
    client = MagicMock()
    client.responses.create.side_effect = RuntimeError(
        "Error code: 429 - You exceeded your current quota; insufficient_quota."
    )
    with pytest.raises(WebSearchQuotaError, match="quota/rate-limit"):
        web_search("q", settings=_settings(), client=client)


def test_websearchhit_is_frozen() -> None:
    hit = WebSearchHit(url="u", title="t", snippet="s")
    with pytest.raises((AttributeError, ValueError)):
        hit.url = "other"  # type: ignore[misc]
