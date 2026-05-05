"""OpenAI Responses API + `web_search` (GA) Tool wrapper.

Returns a frozen Pydantic `WebSearchResult` so trading-agent inference can
log the result deterministically. Errors propagate as `WebSearchError`; no
silent fallback (the caller decides whether to skip the cycle or proceed).

GA `web_search` exposes citations on `output[i].type == "message"` items
under `content[j].annotations[k]` with `type == "url_citation"` and fields
`url`, `title`, `start_index`, `end_index`. The deprecated `web_search_preview`
shape (`web_search_call.results[*].{url,title,snippet}`) is no longer
parsed — see PR #37 for the GA switch.

Phase 6c: every call also writes a best-effort row to ``web_search_calls``
that joins back to ``subagent_runs`` via the ``RUN_ID`` env var the
subagent_runner propagates into the subprocess. Audit-write failures are
logged and swallowed so a hiccupped DB never poisons a healthy search.

P1.x: each successful response also extracts ``response.usage`` token
counts and the number of internal ``web_search_call`` items so the
per-cycle OpenAI cost-report CLI can aggregate exact USD spend. Failed
calls (quota_exhausted / transient) skip cost computation and persist
NULL.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text as sql_text

from research.skills.openai_cost import UnknownModelError, calculate_cost_usd
from shared.db import get_session

if TYPE_CHECKING:
    from decimal import Decimal

    from openai import OpenAI

    from shared.config.settings import Settings

_logger = logging.getLogger(__name__)

_INSERT_WEB_SEARCH_CALL = sql_text(
    """
    INSERT INTO web_search_calls (
        run_id, cycle_id, agent_name, query, summary, hits,
        model_used, elapsed_sec, started_at, finished_at, error, error_type,
        input_tokens, output_tokens, cached_input_tokens, web_search_count, cost_usd
    ) VALUES (
        :run_id, :cycle_id, :agent_name, :query, :summary,
        CAST(:hits AS jsonb),
        :model_used, :elapsed_sec, :started_at, :finished_at, :error, :error_type,
        :input_tokens, :output_tokens, :cached_input_tokens, :web_search_count, :cost_usd
    )
    """
)


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


class _Usage(BaseModel):
    """Token + search counts extracted from a Responses-API response."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    web_search_count: int = 0


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

    started_at = datetime.now(UTC)
    start = time.monotonic()
    summary = ""
    hits: list[WebSearchHit] = []
    error_msg: str | None = None
    error_type: str | None = None
    usage = _Usage()
    cost_usd: Decimal | None = None

    try:
        try:
            response = real_client.responses.create(
                model=settings.OPENAI_MODEL,
                input=query,
                tools=[{"type": "web_search"}],
                timeout=float(settings.WEB_SEARCH_TIMEOUT_SEC),
            )
        except Exception as exc:
            if _is_quota_failure(exc):
                error_msg = f"OpenAI web_search quota/rate-limit refusal: {exc}"
                error_type = "quota_exhausted"
                raise WebSearchQuotaError(error_msg) from exc
            error_msg = f"OpenAI web_search failed: {exc}"
            error_type = "transient"
            raise WebSearchError(error_msg) from exc

        summary = _extract_summary(response)
        hits = _extract_hits(response, settings.WEB_SEARCH_BLOCKED_DOMAINS)
        usage = _extract_usage(response)
        cost_usd = _safe_calculate_cost(settings, usage)
        elapsed = time.monotonic() - start
        return WebSearchResult(
            query=query,
            summary=summary,
            hits=hits,
            elapsed_sec=elapsed,
            model_used=settings.OPENAI_MODEL,
        )
    finally:
        _persist_web_search_call(
            query=query,
            summary=summary,
            hits=hits,
            model_used=settings.OPENAI_MODEL,
            elapsed_sec=time.monotonic() - start,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            error=error_msg,
            error_type=error_type,
            usage=usage,
            cost_usd=cost_usd,
        )


def _safe_calculate_cost(settings: Settings, usage: _Usage) -> Decimal | None:
    """Return cost_usd or None if pricing is missing for the configured model.

    A missing pricing entry is logged once and treated as "unknown cost" —
    the audit row still gets the token counts, the cost_usd column stays
    NULL, and the cost-report CLI surfaces it as ``Unknown`` rather than
    silently summing 0.
    """
    try:
        return calculate_cost_usd(
            model=settings.OPENAI_MODEL,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            web_search_count=usage.web_search_count,
            settings=settings,
        )
    except UnknownModelError:
        _logger.warning(
            "openai_pricing_missing: model=%s — cost_usd will be NULL",
            settings.OPENAI_MODEL,
        )
        return None


def _persist_web_search_call(
    *,
    query: str,
    summary: str,
    hits: list[WebSearchHit],
    model_used: str,
    elapsed_sec: float,
    started_at: datetime,
    finished_at: datetime,
    error: str | None,
    error_type: str | None,
    usage: _Usage,
    cost_usd: Decimal | None,
) -> None:
    """Best-effort INSERT into ``web_search_calls``.

    Reads ``RUN_ID`` / ``CYCLE_ID`` / ``AGENT_NAME`` from the environment —
    the subagent_runner propagates them when it spawns the trading-agent
    subprocess so each search joins back to its parent ``subagent_runs``
    row. If the env var is missing (e.g. the search is invoked outside a
    cycle) the row is still written with NULL keys.

    Token counts and ``cost_usd`` are persisted only for successful calls.
    For failed calls (``error_type`` set) all five usage/cost columns
    stay NULL — there is no real ``response.usage`` to read.
    """
    run_id = os.environ.get("RUN_ID") or None
    cycle_id = os.environ.get("CYCLE_ID") or None
    agent_name = os.environ.get("AGENT_NAME") or None
    persist_usage = error_type is None
    try:
        with get_session("trading_cycle") as session:
            session.execute(
                _INSERT_WEB_SEARCH_CALL,
                {
                    "run_id": run_id,
                    "cycle_id": cycle_id,
                    "agent_name": agent_name,
                    "query": query,
                    "summary": summary or None,
                    "hits": json.dumps([h.model_dump() for h in hits]) if hits else None,
                    "model_used": model_used,
                    "elapsed_sec": elapsed_sec,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "error": error,
                    "error_type": error_type,
                    "input_tokens": usage.input_tokens if persist_usage else None,
                    "output_tokens": usage.output_tokens if persist_usage else None,
                    "cached_input_tokens": usage.cached_input_tokens if persist_usage else None,
                    "web_search_count": usage.web_search_count if persist_usage else None,
                    "cost_usd": cost_usd if persist_usage else None,
                },
            )
    except Exception:
        _logger.warning(
            "audit_persist_failed: web_search_call run_id=%s cycle_id=%s",
            run_id,
            cycle_id,
            exc_info=True,
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


def _extract_usage(response: Any) -> _Usage:
    """Pull token counts + ``web_search_call`` count off a Responses response.

    The Responses GA shape exposes ``response.usage`` with
    ``input_tokens``, ``output_tokens``, and an optional
    ``input_tokens_details.cached_tokens`` for prompt-cache hits. The
    number of internal searches is the count of ``response.output`` items
    whose ``type == "web_search_call"``. All fields default to 0 if the
    SDK shape changes; a malformed response should not crash the persist
    path.
    """
    usage_obj = _attr(response, "usage")
    input_tokens = int(_attr(usage_obj, "input_tokens") or 0)
    output_tokens = int(_attr(usage_obj, "output_tokens") or 0)

    details = _attr(usage_obj, "input_tokens_details")
    cached = int(_attr(details, "cached_tokens") or 0) if details is not None else 0

    output = _attr(response, "output") or []
    search_count = sum(1 for item in output if _attr(item, "type") == "web_search_call")

    return _Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        web_search_count=search_count,
    )


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)
