"""Pricing helper for OpenAI Responses-API web_search calls.

Pure arithmetic — no I/O, no DB, no logging. Settings.OPENAI_PRICING is the
single source of truth for per-model rates; ``calculate_cost_usd`` applies
them to the token + search counts that ``research.skills.web_search``
extracts from each ``response`` object.

Cost formula::

    billable_input        = max(input_tokens - cached_input_tokens, 0)
    cost = billable_input        * input_per_1m_usd        / 1_000_000
         + cached_input_tokens   * cached_input_per_1m_usd / 1_000_000
         + output_tokens         * output_per_1m_usd       / 1_000_000
         + web_search_count      * web_search_per_1k_usd   / 1_000

Cached input tokens are billed at the lower cached rate; only the
*non-cached* portion is billed at the full input rate. ``web_search_count``
is the number of internal searches the Responses API performed for the
call (one ``responses.create`` request can trigger 0..N searches).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.config.settings import Settings

_PER_MILLION = Decimal(1_000_000)
_PER_THOUSAND = Decimal(1_000)


class UnknownModelError(KeyError):
    """Raised when ``calculate_cost_usd`` is called with an unpriced model."""


def calculate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    web_search_count: int,
    settings: Settings,
) -> Decimal:
    """Return the USD cost of one Responses-API call as a Decimal.

    Negative or missing token counts are treated as zero so a flaky
    ``response.usage`` shape never crashes the persist path. The caller
    is expected to skip cost computation entirely for failed calls.
    """
    pricing = settings.OPENAI_PRICING.get(model)
    if pricing is None:
        msg = f"no OPENAI_PRICING entry for model={model!r}"
        raise UnknownModelError(msg)

    input_t = max(int(input_tokens or 0), 0)
    output_t = max(int(output_tokens or 0), 0)
    cached_t = max(int(cached_input_tokens or 0), 0)
    cached_t = min(cached_t, input_t)
    searches = max(int(web_search_count or 0), 0)

    billable_input = input_t - cached_t

    return (
        Decimal(billable_input) * pricing["input_per_1m_usd"] / _PER_MILLION
        + Decimal(cached_t) * pricing["cached_input_per_1m_usd"] / _PER_MILLION
        + Decimal(output_t) * pricing["output_per_1m_usd"] / _PER_MILLION
        + Decimal(searches) * pricing["web_search_per_1k_usd"] / _PER_THOUSAND
    )
