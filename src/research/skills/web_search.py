"""OpenAI Responses API + `web_search` (GA) Tool wrapper.

Returns a frozen Pydantic `WebSearchResult` so trading-agent inference can
log the result deterministically. Errors propagate as `WebSearchError`; no
silent fallback (the caller decides whether to skip the cycle or proceed).

GA `web_search` exposes citations on `output[i].type == "message"` items
under `content[j].annotations[k]` with `type == "url_citation"` and fields
`url`, `title`, `start_index`, `end_index`. The deprecated `web_search_preview`
shape (`web_search_call.results[*].{url,title,snippet}`) is no longer
parsed — see PR #37 for the GA switch.
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


class WebSearchQuotaError(WebSearchError):
    """Raised when OpenAI refuses the call for billing/quota/rate-limit reasons.

    Distinct subclass so the trading-agent (via the MCP server) can tell apart
    a transient search miss from a hard "no more searches this cycle" signal.
    """


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
            tools=[{"type": "web_search"}],
            timeout=float(settings.WEB_SEARCH_TIMEOUT_SEC),
        )
    except Exception as exc:
        if _is_quota_failure(exc):
            msg = f"OpenAI web_search quota/rate-limit refusal: {exc}"
            raise WebSearchQuotaError(msg) from exc
        msg = f"OpenAI web_search failed: {exc}"
        raise WebSearchError(msg) from exc
    elapsed = time.monotonic() - start

    summary = _extract_summary(response)
    hits = _extract_hits(response, settings.WEB_SEARCH_BLOCKED_DOMAINS)
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


def _is_quota_failure(exc: BaseException) -> bool:
    """True if ``exc`` represents an OpenAI billing/quota/rate-limit refusal.

    Uses class name introspection so we don't have to import the optional
    ``openai`` exception types at module scope. ``insufficient_quota`` shows
    up in the body of generic ``BadRequestError`` exceptions, so we also
    string-match the exception message.
    """
    cls_name = type(exc).__name__
    if cls_name in {"RateLimitError", "AuthenticationError", "PermissionDeniedError"}:
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "insufficient_quota",
            "insufficient quota",
            "credit_balance_too_low",
            "credit balance",
            "billing",
        )
    )


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


def _extract_hits(response: Any, blocked_domains: list[str]) -> list[WebSearchHit]:
    output = getattr(response, "output", None) or []
    hits: list[WebSearchHit] = []
    seen: set[str] = set()
    for item in output:
        if _attr(item, "type") != "message":
            continue
        for chunk in _attr(item, "content") or []:
            for ann in _attr(chunk, "annotations") or []:
                if _attr(ann, "type") != "url_citation":
                    continue
                url = _attr(ann, "url")
                if not url or url in seen:
                    continue
                if _is_blocked(str(url), blocked_domains):
                    continue
                seen.add(url)
                title = _attr(ann, "title") or url
                hits.append(
                    WebSearchHit(
                        url=str(url),
                        title=str(title),
                        snippet="",
                        cited_in_summary=True,
                    ),
                )
    return hits


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
