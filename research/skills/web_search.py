"""Web-search skill — PA-aligned wrapper around OpenAI's web-search tool.

Mirrors Prediction Arena's methodology (predictionarena.ai/methodology):
- Backed by OpenAI's web-search API (Responses API + `web_search_preview`).
- Per-call timeout (configurable, default 120s, central settings §21).
- Site blacklist enforced both via system instruction and post-hoc URL filter.
- Returns a synthesized factual answer plus citations from top sources.

Exposed for trading agents via the Anthropic Messages API tool-use schema
(`WEB_SEARCH_TOOL_SCHEMA`). The `execute_tool_call` adapter is the bridge
called from the agent's tool-dispatch loop.

Per data_infrastructure.md §0: this skill is intentionally a thin wrapper
on top of the official `openai` SDK rather than a custom search-engine impl.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI

from shared.config.settings import Settings, get_settings

# Tool-use schema exposed to Anthropic Claude agents (Messages API).
# Description deliberately conveys the PA "use sparingly" disposition —
# decisions are based on available information, not exhaustive research.
WEB_SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "web_search",
    "description": (
        "Search the web for context on a market event, candidate, news item, or "
        "claim. Returns a concise synthesis with citations from top sources. "
        "Use sparingly — each call has a hard timeout and you should reach a "
        "decision based on available information, not exhaustively research "
        "every detail. Prefer one or two well-formed queries over many narrow ones."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query, in plain language.",
            },
        },
        "required": ["query"],
    },
}


class WebSearchError(RuntimeError):
    """Raised when the upstream web-search call fails or input is invalid."""


@dataclass(slots=True, frozen=True)
class WebSearchCitation:
    title: str
    url: str
    snippet: str = ""


@dataclass(slots=True)
class WebSearchResult:
    query: str
    answer: str
    citations: list[WebSearchCitation] = field(default_factory=list)
    blocked_urls: list[str] = field(default_factory=list)


def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def _is_blacklisted(url: str, blacklist: list[str]) -> bool:
    host = _domain(url)
    if not host:
        return False
    return any(host == b or host.endswith("." + b) for b in (d.lower() for d in blacklist))


def _build_prompt(query: str, blacklist: list[str]) -> str:
    base = (
        f"Search the web for: {query.strip()}\n\n"
        "Return a concise factual synthesis with inline citations to "
        "reputable primary sources. If sources disagree, surface the "
        "disagreement explicitly."
    )
    if blacklist:
        domains = ", ".join(sorted(blacklist))
        base += f"\n\nDo not cite or quote content from these domains: {domains}."
    return base


def _extract_citations(response: Any) -> list[WebSearchCitation]:
    """Pull URL citations out of an OpenAI Responses-API result.

    The Responses API attaches `url_citation` annotations to text content
    blocks. Schema can shift between SDK versions, so we duck-type defensively.
    """
    citations: list[WebSearchCitation] = []
    output = getattr(response, "output", None) or []
    for item in output:
        content = getattr(item, "content", None) or []
        for block in content:
            for ann in getattr(block, "annotations", None) or []:
                if getattr(ann, "type", "") != "url_citation":
                    continue
                url = getattr(ann, "url", "") or ""
                if not url:
                    continue
                citations.append(
                    WebSearchCitation(
                        title=getattr(ann, "title", "") or "",
                        url=url,
                    )
                )
    return citations


def web_search(
    query: str,
    *,
    client: OpenAI | None = None,
    settings: Settings | None = None,
) -> WebSearchResult:
    """Run a single web-search call and return the synthesized result.

    Args:
        query: The search query in plain language.
        client: Optional pre-built OpenAI client (used by tests).
        settings: Optional Settings override (used by tests).

    Raises:
        WebSearchError: on empty query or upstream API failure.
    """
    if not query.strip():
        raise WebSearchError("Empty query.")

    cfg = settings or get_settings()
    if not cfg.openai_api_key:
        raise WebSearchError("OPENAI_API_KEY not configured.")

    oai = client or OpenAI(api_key=cfg.openai_api_key, timeout=cfg.web_search_timeout_s)
    prompt = _build_prompt(query, cfg.web_search_blacklist)

    try:
        response = oai.responses.create(
            model=cfg.openai_web_search_model,
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )
    except Exception as exc:
        raise WebSearchError(f"OpenAI web-search call failed: {exc}") from exc

    answer = (getattr(response, "output_text", "") or "").strip()
    raw_citations = _extract_citations(response)

    citations: list[WebSearchCitation] = []
    blocked: list[str] = []
    for c in raw_citations:
        if _is_blacklisted(c.url, cfg.web_search_blacklist):
            blocked.append(c.url)
            continue
        citations.append(c)

    return WebSearchResult(
        query=query,
        answer=answer,
        citations=citations,
        blocked_urls=blocked,
    )


def execute_tool_call(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Dispatch entry point used by the agent tool-use loop.

    Accepts the `input` dict from an Anthropic `tool_use` block; returns a
    JSON-serializable payload to be wrapped in a `tool_result` block.
    """
    query = tool_input.get("query")
    if not isinstance(query, str):
        raise WebSearchError("`query` must be a string.")
    result = web_search(query)
    return {
        "answer": result.answer,
        "citations": [
            {"title": c.title, "url": c.url, "snippet": c.snippet} for c in result.citations
        ],
        "blocked_urls": result.blocked_urls,
    }
