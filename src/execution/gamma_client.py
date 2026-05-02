"""Read-only Polymarket Gamma API client (used by outcome_ingestion only).

Authority boundary per `specs/trading_feedback.md`: this module **never** imports
`shared.adapters.polymarket` or `py_clob_client`, holds no signing keys, and
has no write methods. The outcome_ingestion script binds to the
`outcome_ingestion` Postgres role which has UPDATE rights only on a small
column subset (predictions.outcome/realized_pnl, trades.realized_pnl, ...).

Endpoint: `https://gamma-api.polymarket.com`. We use a sync `httpx.Client`
with retry on 5xx/429 and explicit JSON parsing — no SDK dependency.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from shared.models import Resolution

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

DEFAULT_HOST: Final = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT_SEC: Final = 20.0
DEFAULT_PAGE_SIZE: Final = 100
MIN_PRICE_PAIR_LEN: Final = 2
YES_PRICE_THRESHOLD: Final = 0.5


class GammaClientError(RuntimeError):
    """Gamma API call failed (network, 5xx, malformed JSON)."""


class GammaClient:
    """Tiny sync wrapper for the Gamma `markets` endpoint (read-only)."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        client: httpx.Client | None = None,
    ) -> None:
        self._host = host
        self._client = client or httpx.Client(timeout=timeout_sec)

    def list_resolved_markets(self, *, since: datetime, page_size: int = DEFAULT_PAGE_SIZE) -> Iterator[dict[str, Any]]:
        """Yield raw Gamma market dicts whose resolution is newer than `since`."""
        offset = 0
        while True:
            params = {
                "closed": "true",
                "limit": str(page_size),
                "offset": str(offset),
                "ascending": "false",
            }
            payload = self._get_json("/markets", params=params)
            items = _coerce_list(payload)
            if not items:
                return
            count = 0
            for item in items:
                resolved_at = _parse_dt(item.get("end_date_iso") or item.get("endDate") or item.get("resolved_at"))
                if resolved_at and resolved_at >= since:
                    yield item
                    count += 1
            if len(items) < page_size:
                return
            offset += page_size
            if count == 0:
                return  # everything in this page is older than `since`

    def get_resolution(self, market_id: str) -> Resolution | None:
        """Fetch the settled outcome for a market, or None if not resolved yet."""
        payload = self._get_json(f"/markets/{market_id}")
        if not isinstance(payload, dict):
            return None
        return _parse_resolution(market_id, payload)

    def close(self) -> None:
        self._client.close()

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        url = f"{self._host}{path}"
        try:
            response = self._client.get(url, params=params or {})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            msg = f"Gamma {path} returned HTTP {exc.response.status_code}"
            raise GammaClientError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"Gamma {path} network error: {exc}"
            raise GammaClientError(msg) from exc
        try:
            return response.json()
        except ValueError as exc:
            msg = f"Gamma {path} returned malformed JSON"
            raise GammaClientError(msg) from exc


def _coerce_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "markets", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _parse_resolution(market_id: str, item: dict[str, Any]) -> Resolution | None:
    if not item.get("closed") and item.get("outcome") is None:
        return None
    outcome_raw = item.get("outcome")
    if outcome_raw is None and item.get("outcomePrices"):
        prices = item["outcomePrices"]
        if isinstance(prices, list) and len(prices) >= MIN_PRICE_PAIR_LEN:
            try:
                yes_price = float(prices[0])
                outcome_raw = yes_price > YES_PRICE_THRESHOLD
            except (ValueError, TypeError):
                outcome_raw = None
    if outcome_raw is None:
        return None
    outcome = bool(outcome_raw) if not isinstance(outcome_raw, str) else outcome_raw.lower() == "yes"
    resolved_at = _parse_dt(item.get("resolved_at") or item.get("end_date_iso") or item.get("endDate"))
    if resolved_at is None:
        resolved_at = datetime.now(UTC)
    return Resolution(
        market_id=market_id,
        outcome=outcome,
        resolved_at=resolved_at,
        settlement_price=1.0 if outcome else 0.0,
        disputed=bool(item.get("disputed", False)),
    )
