"""Tests for the local research MCP server (Phase 6b PR 2).

We exercise the server's tool handlers directly by introspecting the
``Server`` instance built by ``build_server``. This avoids spawning a
subprocess + stdio transport in CI while still verifying the schema and
the OpenAI-backed handler returns serialised ``WebSearchResult`` JSON.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

from research.skills import mcp_server
from research.skills.web_search import WebSearchError
from shared.config.settings import Settings


def _settings_with_key() -> Settings:
    return Settings(OPENAI_API_KEY="test-key")


def _stub_openai_response(summary: str, hits: list[dict[str, str]] | None = None) -> MagicMock:
    """Stub a GA `web_search` response with `message.content[*].annotations`."""
    response = MagicMock()
    response.output_text = summary
    if hits is None:
        response.output = []
        return response
    annotations = [MagicMock(type="url_citation", url=h["url"], title=h.get("title", h["url"])) for h in hits]
    chunk = MagicMock(text=summary, annotations=annotations)
    message = MagicMock(type="message", content=[chunk])
    response.output = [message]
    return response


def _build_stub_client(response: Any) -> MagicMock:
    client = MagicMock()
    client.responses.create.return_value = response
    return client


def _list_tools_handler(server: Any) -> Any:
    return server.request_handlers[next(k for k in server.request_handlers if k.__name__ == "ListToolsRequest")]


def _call_tool_handler(server: Any) -> Any:
    return server.request_handlers[next(k for k in server.request_handlers if k.__name__ == "CallToolRequest")]


def test_list_tools_advertises_web_search() -> None:
    server = mcp_server.build_server(settings=_settings_with_key())
    handler = _list_tools_handler(server)
    result = asyncio.run(handler(MagicMock()))
    tools = result.root.tools
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "web_search"
    assert tool.inputSchema["required"] == ["query"]


def test_call_tool_returns_serialized_web_search_result() -> None:
    response = _stub_openai_response(
        "Bitcoin closed up.",
        hits=[{"url": "https://example.com/btc", "title": "BTC News"}],
    )
    client = _build_stub_client(response)
    server = mcp_server.build_server(settings=_settings_with_key(), openai_client=client)

    handler = _call_tool_handler(server)
    request = MagicMock()
    request.params.name = "web_search"
    request.params.arguments = {"query": "Will BTC close above 100k?"}
    result = asyncio.run(handler(request))
    contents = result.root.content
    assert len(contents) == 1
    payload = json.loads(contents[0].text)
    assert payload["query"] == "Will BTC close above 100k?"
    assert payload["summary"] == "Bitcoin closed up."
    assert payload["hits"][0]["url"] == "https://example.com/btc"


def test_call_tool_unknown_name_raises() -> None:
    server = mcp_server.build_server(settings=_settings_with_key())
    handler = _call_tool_handler(server)
    request = MagicMock()
    request.params.name = "unknown"
    request.params.arguments = {"query": "x"}
    result = asyncio.run(handler(request))
    assert result.root.isError is True


def test_call_tool_missing_query_raises() -> None:
    server = mcp_server.build_server(settings=_settings_with_key())
    handler = _call_tool_handler(server)
    request = MagicMock()
    request.params.name = "web_search"
    request.params.arguments = {}
    result = asyncio.run(handler(request))
    assert result.root.isError is True


def test_call_tool_returns_error_payload_when_web_search_fails() -> None:
    settings = Settings(OPENAI_API_KEY="")  # no OPENAI_API_KEY → WebSearchError
    server = mcp_server.build_server(settings=settings)

    handler = _call_tool_handler(server)
    request = MagicMock()
    request.params.name = "web_search"
    request.params.arguments = {"query": "anything"}
    result = asyncio.run(handler(request))
    contents = result.root.content
    payload = json.loads(contents[0].text)
    assert "error" in payload
    assert "OPENAI_API_KEY" in payload["error"]
    assert payload["error_type"] == "transient"


def test_call_tool_marks_quota_error_in_payload() -> None:
    class _RateLimitError(Exception):
        pass

    client = MagicMock()
    client.responses.create.side_effect = _RateLimitError("Rate limit exceeded")
    settings = Settings(OPENAI_API_KEY="test-key")
    server = mcp_server.build_server(settings=settings, openai_client=client)

    handler = _call_tool_handler(server)
    request = MagicMock()
    request.params.name = "web_search"
    request.params.arguments = {"query": "anything"}
    result = asyncio.run(handler(request))
    payload = json.loads(result.root.content[0].text)
    assert payload["error_type"] == "transient"

    client.responses.create.side_effect = RuntimeError("insufficient_quota")
    result = asyncio.run(handler(request))
    payload = json.loads(result.root.content[0].text)
    assert payload["error_type"] == "quota_exhausted"


def test_web_search_error_is_caught_and_serialised() -> None:
    """Smoke that the WebSearchError import works in this module."""
    err = WebSearchError("boom")
    assert "boom" in str(err)


def test_call_tool_strips_blocked_domains_from_hits() -> None:
    response = _stub_openai_response(
        "Bitcoin closed up.",
        hits=[
            {"url": "https://pro.coinmarketcap.com/btc", "title": "CMC"},
            {"url": "https://www.bloomberg.com/btc", "title": "Bloomberg"},
        ],
    )
    client = _build_stub_client(response)
    settings = Settings(OPENAI_API_KEY="test-key", WEB_SEARCH_BLOCKED_DOMAINS=["coinmarketcap.com"])
    server = mcp_server.build_server(settings=settings, openai_client=client)

    handler = _call_tool_handler(server)
    request = MagicMock()
    request.params.name = "web_search"
    request.params.arguments = {"query": "Will BTC close above 100k?"}
    result = asyncio.run(handler(request))
    payload = json.loads(result.root.content[0].text)
    urls = [h["url"] for h in payload["hits"]]
    assert urls == ["https://www.bloomberg.com/btc"]
