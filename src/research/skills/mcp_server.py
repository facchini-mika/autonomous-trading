"""Local MCP server exposing the OpenAI-backed web_search tool.

Phase 6b PR 2 wires this server into the trading-agent subagent invocation
so the LLM can call ``mcp__research__web_search`` and receive an
OpenAI-Responses-API-backed answer with citations. Scanner-reviewer and
risk-execution do not get the MCP config; they remain deterministic.

Run as ``uv run python -m research.skills.mcp_server``; the
``.mcp.json`` at the repo root registers this command under server name
``research``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from research.skills.web_search import WebSearchError, WebSearchQuotaError, web_search
from shared.config.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from openai import OpenAI

logger = logging.getLogger(__name__)

SERVER_NAME = "research"
TOOL_NAME = "web_search"

WEB_SEARCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query — usually a market resolution question + context.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

WEB_SEARCH_DESCRIPTION = (
    "OpenAI Responses API web search. Returns a JSON object with `summary`, `hits` "
    "(url+title+snippet+cited_in_summary), `elapsed_sec`, and `model_used`. "
    "Use sparingly — every call costs OpenAI tokens and is bounded by "
    "Settings.WEB_SEARCH_TIMEOUT_SEC."
)


def build_server(
    *,
    settings: Settings | None = None,
    openai_client: OpenAI | None = None,
) -> Server[Any, Any]:
    """Construct the stdio-MCP server. Tests inject ``settings`` + ``openai_client``."""
    cfg = settings or Settings()
    server: Server[Any, Any] = Server(SERVER_NAME)

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name=TOOL_NAME,
                description=WEB_SEARCH_DESCRIPTION,
                inputSchema=WEB_SEARCH_INPUT_SCHEMA,
            ),
        ]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name != TOOL_NAME:
            msg = f"unknown tool: {name}"
            raise ValueError(msg)
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            msg = "tool arguments must include a non-empty `query` string"
            raise ValueError(msg)
        try:
            result = await asyncio.to_thread(web_search, query, settings=cfg, client=openai_client)
        except WebSearchQuotaError as exc:
            payload = {"error": str(exc), "error_type": "quota_exhausted", "query": query}
            return [TextContent(type="text", text=json.dumps(payload))]
        except WebSearchError as exc:
            payload = {"error": str(exc), "error_type": "transient", "query": query}
            return [TextContent(type="text", text=json.dumps(payload))]
        return [TextContent(type="text", text=result.model_dump_json())]

    return server


async def _serve(
    *,
    server_factory: Callable[[], Server[Any, Any]] | None = None,
    transport_factory: Callable[[], Any] | None = None,
) -> None:
    server = (server_factory or build_server)()
    transport = transport_factory() if transport_factory is not None else stdio_server()
    init_options = server.create_initialization_options()
    async with transport as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
