"""Unit tests for the web-search skill (no real OpenAI calls)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from research.skills.web_search import (
    WEB_SEARCH_TOOL_SCHEMA,
    WebSearchError,
    _domain,
    _is_blacklisted,
    execute_tool_call,
    web_search,
)
from shared.config.settings import Settings


def _make_response(answer: str, citations: list[tuple[str, str]]) -> Any:
    """Build a duck-typed object that mimics the OpenAI Responses-API result."""
    annotations = [
        SimpleNamespace(type="url_citation", url=url, title=title) for title, url in citations
    ]
    block = SimpleNamespace(annotations=annotations)
    item = SimpleNamespace(content=[block])
    return SimpleNamespace(output_text=answer, output=[item])


class _StubResponses:
    def __init__(self, answer: str, citations: list[tuple[str, str]]):
        self._answer = answer
        self._citations = citations
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _make_response(self._answer, self._citations)


class _StubClient:
    def __init__(self, answer: str, citations: list[tuple[str, str]]):
        self.responses = _StubResponses(answer, citations)


def _settings(blacklist: list[str] | None = None) -> Settings:
    return Settings(
        openai_api_key="sk-test",
        web_search_blacklist=blacklist if blacklist is not None else ["coinmarketcap.com"],
    )


def test_tool_schema_has_required_fields() -> None:
    assert WEB_SEARCH_TOOL_SCHEMA["name"] == "web_search"
    schema = WEB_SEARCH_TOOL_SCHEMA["input_schema"]
    assert schema["required"] == ["query"]
    assert "query" in schema["properties"]


def test_empty_query_rejected() -> None:
    with pytest.raises(WebSearchError, match="Empty query"):
        web_search("   ", settings=_settings())


def test_missing_api_key_rejected() -> None:
    cfg = Settings(openai_api_key="")
    with pytest.raises(WebSearchError, match="OPENAI_API_KEY"):
        web_search("anything", settings=cfg)


def test_returns_answer_and_citations() -> None:
    client = _StubClient(
        answer="Synthesized answer.",
        citations=[("Reuters", "https://reuters.com/article"), ("AP", "https://apnews.com/x")],
    )
    result = web_search("election news", client=client, settings=_settings())
    assert result.answer == "Synthesized answer."
    assert [c.url for c in result.citations] == [
        "https://reuters.com/article",
        "https://apnews.com/x",
    ]
    assert result.blocked_urls == []


def test_blacklisted_domain_filtered() -> None:
    client = _StubClient(
        answer="Mixed sources.",
        citations=[
            ("CMC", "https://coinmarketcap.com/currencies/btc"),
            ("CoinDesk", "https://coindesk.com/markets/btc"),
            ("CMC subdomain", "https://www.coinmarketcap.com/markets"),
        ],
    )
    result = web_search("btc price", client=client, settings=_settings())
    assert [c.url for c in result.citations] == ["https://coindesk.com/markets/btc"]
    assert len(result.blocked_urls) == 2


def test_blacklist_instruction_passed_to_model() -> None:
    client = _StubClient(answer="ok", citations=[])
    web_search("x", client=client, settings=_settings(blacklist=["bad.example"]))
    call = client.responses.calls[0]
    assert "bad.example" in call["input"]
    assert call["tools"] == [{"type": "web_search_preview"}]


def test_execute_tool_call_validates_input(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(WebSearchError, match="must be a string"):
        execute_tool_call({"query": 123})


def test_execute_tool_call_dispatches_to_web_search(monkeypatch: pytest.MonkeyPatch) -> None:
    from research.skills import web_search as ws

    captured: dict[str, Any] = {}

    def fake(query: str) -> ws.WebSearchResult:
        captured["query"] = query
        return ws.WebSearchResult(
            query=query,
            answer="hi",
            citations=[ws.WebSearchCitation(title="T", url="https://t.example", snippet="s")],
            blocked_urls=["https://blocked.example"],
        )

    monkeypatch.setattr(ws, "web_search", fake)
    payload = execute_tool_call({"query": "hello"})
    assert captured["query"] == "hello"
    assert payload["answer"] == "hi"
    assert payload["citations"] == [{"title": "T", "url": "https://t.example", "snippet": "s"}]
    assert payload["blocked_urls"] == ["https://blocked.example"]


def test_domain_extraction() -> None:
    assert _domain("https://www.example.com/path") == "example.com"
    assert _domain("http://sub.example.com/x") == "sub.example.com"
    assert _domain("not a url") == ""


def test_is_blacklisted_matches_subdomains() -> None:
    bl = ["coinmarketcap.com"]
    assert _is_blacklisted("https://coinmarketcap.com/x", bl)
    assert _is_blacklisted("https://www.coinmarketcap.com/x", bl)
    assert _is_blacklisted("https://api.coinmarketcap.com/x", bl)
    assert not _is_blacklisted("https://coindesk.com/x", bl)
    assert not _is_blacklisted("https://fakecoinmarketcap.com/x", bl)
