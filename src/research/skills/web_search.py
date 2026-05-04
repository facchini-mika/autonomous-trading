"""OpenAI Responses API + `web_search_preview` Tool wrapper.

Returns a frozen Pydantic `WebSearchResult` so trading-agent inference can
log the result deterministically. Errors propagate as `WebSearchError`; no
silent fallback (the caller decides whether to skip the cycle or proceed).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from openai import OpenAI

    from shared.config.settings import Settings


class WebSearchError(RuntimeError):
    """Raised when the OpenAI call fails or returns an unparseable response."""


class WebSearchHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    title: str
    snippet: str
    cited_in_summary: bool = False


class WebSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    summary: str
    hits: list[WebSearchHit] = Field(default_factory=list)
    elapsed_sec: float = Field(ge=0.0)
    model_used: str


def web_search(
    query: str,
    *,
    settings: Settings,
    client: OpenAI | None = None,
) -> WebSearchResult:
    if not settings.OPENAI_API_KEY:
        msg = "OPENAI_API_KEY is required to call web_search"
        raise WebSearchError(msg)

    real_client = client or _default_client(settings)

    start = time.monotonic()
    try:
        response = real_client.responses.create(
            model=settings.OPENAI_MODEL,
            input=query,
            tools=[{"type": "web_search_preview"}],
            timeout=float(settings.WEB_SEARCH_TIMEOUT_SEC),
        )
    except Exception as exc:
        msg = f"OpenAI web_search failed: {exc}"
        raise WebSearchError(msg) from exc
    elapsed = time.monotonic() - start

    summary = _extract_summary(response)
    hits = _extract_hits(response, summary, settings.WEB_SEARCH_BLOCKED_DOMAINS)
    return WebSearchResult(
        query=query,
        summary=summary,
        hits=hits,
        elapsed_sec=elapsed,
        model_used=settings.OPENAI_MODEL,
    )


def _default_client(settings: Settings) -> OpenAI:
    from openai import OpenAI  # noqa: PLC0415

    return OpenAI(api_key=settings.OPENAI_API_KEY)


def _extract_summary(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return str(text)
    output = getattr(response, "output", None) or []
    parts: list[str] = []
    for item in output:
        content = getattr(item, "content", None) or item.get("content") if isinstance(item, dict) else None
        if not content:
            continue
        for chunk in content:
            chunk_text = getattr(chunk, "text", None) or (chunk.get("text") if isinstance(chunk, dict) else None)
            if chunk_text:
                parts.append(str(chunk_text))
    return "\n".join(parts)


def _is_blocked(url: str, blocked_domains: list[str]) -> bool:
    """True if the URL's host equals or is a subdomain of any blocked entry.

    Case-insensitive. Empty/malformed URLs and an empty blocklist return False.
    """
    if not blocked_domains or not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in blocked_domains if d)


def _extract_hits(response: Any, summary: str, blocked_domains: list[str]) -> list[WebSearchHit]:
    output = getattr(response, "output", None) or []
    hits: list[WebSearchHit] = []
    for item in output:
        item_type = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
        if item_type == "web_search_call":
            results = getattr(item, "results", None) or (item.get("results") if isinstance(item, dict) else None) or []
            for res in results:
                url = _attr(res, "url")
                title = _attr(res, "title") or url
                snippet = _attr(res, "snippet") or ""
                if url and not _is_blocked(str(url), blocked_domains):
                    hits.append(
                        WebSearchHit(
                            url=str(url),
                            title=str(title),
                            snippet=str(snippet),
                            cited_in_summary=str(url) in summary,
                        ),
                    )
    return hits


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
