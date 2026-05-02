"""PolymarketAdapter — sync REST wrapper around `py-clob-client`.

Phase-4 Modus 1: a paket-private hook on `KeyProviderLocalFile`
(`_unsafe_export_priv_key`) hands the raw private key to py-clob-client which
performs EIP-712 signing internally. The KeyProvider Protocol stays intact for
KMS migration in Phase 7 — see `docs/adr/0001-keyprovider-pyclobclient-mode-1.md`.

WebSocket subscriptions are deliberately out of scope for Phase 4. The trading
cycle period is 12 minutes x at most 50 markets, so REST polling is trivially
sufficient. Phase 5+ may layer a websocket cache on top.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, TypeVar

from shared.models import (
    CancelResult,
    Market,
    MarketMetadata,
    Order,
    Orderbook,
    OrderResult,
    Resolution,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from shared.adapters.key_provider import KeyProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_HOST: Final = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID: Final = 137
RETRY_ATTEMPTS: Final = 3
RETRY_BACKOFF_BASE: Final = 0.25
RETRY_BACKOFF_FACTOR: Final = 4.0
RETRY_JITTER_PCT: Final = 0.25


class PolymarketAdapterError(RuntimeError):
    """Base exception for the Polymarket adapter."""


class PolymarketTransientError(PolymarketAdapterError):
    """Retryable: 408/429/5xx, network timeout, connection reset."""


class PolymarketPermanentError(PolymarketAdapterError):
    """Non-retryable: 4xx other than 408/429 (validation, auth, balance)."""


class PolymarketRateLimitError(PolymarketTransientError):
    """Specifically a 429 response — retry after backoff."""


class IdempotencyStore(Protocol):
    """Backing store for `idempotency_key -> OrderResult` cache."""

    def get(self, key: str) -> OrderResult | None: ...
    def put(self, key: str, value: OrderResult) -> None: ...


class InMemoryIdempotencyStore:
    """Simple dict-backed store, lifetime = process lifetime.

    Phase 4 trading cycles are single-process; Phase 5 may swap in a Postgres
    implementation when cron splits cycles across pods.
    """

    def __init__(self) -> None:
        self._cache: dict[str, OrderResult] = {}

    def get(self, key: str) -> OrderResult | None:
        return self._cache.get(key)

    def put(self, key: str, value: OrderResult) -> None:
        self._cache[key] = value


class PolymarketAdapter:
    """Adapter satisfying `PredictionMarketAdapter` against the Polymarket CLOB."""

    def __init__(
        self,
        *,
        key_provider: KeyProvider,
        host: str = DEFAULT_HOST,
        chain_id: int = DEFAULT_CHAIN_ID,
        idempotency_store: IdempotencyStore | None = None,
        clock: Callable[[], datetime] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._key_provider = key_provider
        self._host = host
        self._chain_id = chain_id
        self._idempotency = idempotency_store or InMemoryIdempotencyStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = self._make_client(client_factory)
        self._api_creds_set = False

    # --- Reads ---------------------------------------------------------------

    def get_markets(self, *, limit: int) -> list[Market]:
        raw = self._retry(self._client.get_markets, op="get_markets")
        markets = _parse_markets_response(raw, clock=self._clock)
        return markets[:limit]

    def get_orderbook(self, market_id: str) -> Orderbook:
        raw = self._retry(lambda: self._client.get_order_book(market_id), op="get_order_book")
        return _parse_orderbook(market_id, raw, clock=self._clock)

    def get_metadata(self, market_id: str) -> MarketMetadata:
        raw = self._retry(lambda: self._client.get_market(market_id), op="get_market")
        return _parse_metadata(market_id, raw)

    def get_resolution(self, market_id: str) -> Resolution | None:
        raw = self._retry(lambda: self._client.get_market(market_id), op="get_market")
        return _parse_resolution(market_id, raw)

    # --- Writes --------------------------------------------------------------

    def place_order(self, order: Order) -> OrderResult:
        cached = self._idempotency.get(order.idempotency_key)
        if cached is not None:
            logger.debug("Idempotency hit for key=%s; returning cached result", order.idempotency_key)
            return cached

        self._ensure_api_creds()

        try:
            raw = self._retry(lambda: self._submit_order(order), op="post_order")
        except PolymarketPermanentError as exc:
            result = OrderResult(status="rejected", broker_order_id=None, fill_price=None, filled_size=0.0)
            logger.warning("Permanent error placing order %s: %s", order.idempotency_key, exc)
            self._idempotency.put(order.idempotency_key, result)
            return result

        result = _parse_order_result(raw)
        self._idempotency.put(order.idempotency_key, result)
        return result

    def cancel_order(self, order_id: str) -> CancelResult:
        self._ensure_api_creds()
        try:
            raw = self._retry(lambda: self._client.cancel(order_id=order_id), op="cancel_order")
        except PolymarketPermanentError as exc:
            if _is_not_found(exc):
                return CancelResult(order_id=order_id, status="not_found", cancelled_size=0.0)
            return CancelResult(order_id=order_id, status="error", cancelled_size=0.0)
        return _parse_cancel_result(order_id, raw)

    # --- Internals -----------------------------------------------------------

    def _make_client(self, factory: Callable[..., Any] | None) -> Any:
        if factory is not None:
            return factory(host=self._host, chain_id=self._chain_id, key_provider=self._key_provider)
        from py_clob_client.client import ClobClient  # noqa: PLC0415

        from shared.adapters.key_provider_localfile import (  # noqa: PLC0415
            KeyProviderLocalFile,
        )

        if not isinstance(self._key_provider, KeyProviderLocalFile):
            msg = "PolymarketAdapter Modus 1 requires KeyProviderLocalFile (raw-key export)"
            raise TypeError(msg)
        priv_hex = self._key_provider._unsafe_export_priv_key().hex()  # noqa: SLF001
        return ClobClient(self._host, key=priv_hex, chain_id=self._chain_id)

    def _ensure_api_creds(self) -> None:
        if self._api_creds_set:
            return
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)
        self._api_creds_set = True

    def _submit_order(self, order: Order) -> Any:
        signed = self._client.create_order(_to_clob_order_args(order))
        return self._client.post_order(signed)

    def _retry(self, fn: Callable[[], T], *, op: str) -> T:
        last_exc: BaseException | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return fn()
            except PolymarketTransientError as exc:
                last_exc = exc
                wait = _backoff_for(attempt)
                logger.info(
                    "Transient error in %s (attempt %d/%d): %s; backing off %.2fs",
                    op,
                    attempt + 1,
                    RETRY_ATTEMPTS,
                    exc,
                    wait,
                )
                time.sleep(wait)
            except PolymarketPermanentError:
                raise
            except Exception as exc:
                mapped = _map_unknown(exc)
                if isinstance(mapped, PolymarketTransientError):
                    last_exc = mapped
                    wait = _backoff_for(attempt)
                    logger.info("Mapped transient error in %s: %s; backing off %.2fs", op, exc, wait)
                    time.sleep(wait)
                    continue
                raise mapped from exc
        if last_exc is None:
            msg = "Retry loop exited without result; this is a bug"
            raise PolymarketAdapterError(msg)
        raise last_exc


def _backoff_for(attempt: int) -> float:
    base = RETRY_BACKOFF_BASE * (RETRY_BACKOFF_FACTOR**attempt)
    jitter = random.uniform(-RETRY_JITTER_PCT, RETRY_JITTER_PCT) * base  # noqa: S311
    return max(0.0, base + jitter)


def _map_unknown(exc: BaseException) -> PolymarketAdapterError:
    """Best-effort mapping for unexpected exceptions surfaced by py-clob-client."""
    text = str(exc).lower()
    if any(kw in text for kw in ("timeout", "connection reset", "connection aborted", "5xx", "503", "502", "504")):
        return PolymarketTransientError(str(exc))
    if "429" in text or "rate limit" in text:
        return PolymarketRateLimitError(str(exc))
    if any(kw in text for kw in ("400", "401", "403", "404", "validation")):
        return PolymarketPermanentError(str(exc))
    return PolymarketTransientError(str(exc))


def _is_not_found(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text


def _parse_markets_response(raw: Any, *, clock: Callable[[], datetime]) -> list[Market]:
    items = (raw.get("data") or raw.get("markets") or []) if isinstance(raw, dict) else list(raw or [])
    out: list[Market] = []
    for item in items:
        try:
            out.append(_parse_market_item(item, clock=clock))
        except (KeyError, ValueError, TypeError):
            logger.debug("Skipping unparseable market item: %r", item)
    return out


def _parse_market_item(item: dict[str, Any], *, clock: Callable[[], datetime]) -> Market:
    return Market(
        market_id=str(item.get("condition_id") or item.get("market_id") or item["id"]),
        condition_id=str(item.get("condition_id") or item["id"]),
        slug=str(item.get("market_slug") or item.get("slug") or ""),
        title=str(item.get("question") or item.get("title") or ""),
        category=str(item.get("category") or "uncategorized"),
        end_date=_parse_dt(item.get("end_date_iso") or item.get("endDate") or item.get("end_date")) or clock(),
        status=_market_status(item),
        resolution_source=item.get("resolution_source") or None,
        created_at=_parse_dt(item.get("created_at") or item.get("createdAt")) or clock(),
        last_seen=clock(),
        ambiguity_score=None,
    )


def _market_status(item: dict[str, Any]) -> str:
    if item.get("closed"):
        return "closed"
    if item.get("resolved") or (item.get("end_date_iso") and item.get("outcome") is not None):
        return "resolved"
    return "open"


def _parse_orderbook(market_id: str, raw: Any, *, clock: Callable[[], datetime]) -> Orderbook:
    bids = _levels(raw, "bids")
    asks = _levels(raw, "asks")
    best_bid = bids[0][0] if bids else 0.0
    best_ask = asks[0][0] if asks else 1.0
    mid = (best_bid + best_ask) / 2 if bids and asks else max(best_bid, best_ask)
    return Orderbook(
        market_id=market_id,
        best_bid=_clip01(best_bid),
        best_ask=_clip01(best_ask),
        mid=_clip01(mid),
        depth_bid_1pct=_depth_within(bids, best_bid, side="bid"),
        depth_ask_1pct=_depth_within(asks, best_ask, side="ask"),
        timestamp=clock(),
    )


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _levels(raw: Any, side: str) -> list[tuple[float, float]]:
    src = raw.get(side, []) if isinstance(raw, dict) else []
    out: list[tuple[float, float]] = []
    for level in src:
        try:
            if isinstance(level, dict):
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
            else:
                price = float(level[0])
                size = float(level[1])
            out.append((price, size))
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    out.sort(key=lambda x: x[0], reverse=(side == "bids"))
    return out


def _depth_within(levels: list[tuple[float, float]], reference: float, *, side: str) -> float:
    if not levels:
        return 0.0
    bound = reference * 0.99 if side == "bid" else reference * 1.01
    total = 0.0
    for price, size in levels:
        if (side == "bid" and price >= bound) or (side == "ask" and price <= bound):
            total += size
    return total


def _parse_metadata(market_id: str, raw: Any) -> MarketMetadata:
    item = raw if isinstance(raw, dict) else {}
    implied = item.get("implied_probability_yes")
    if implied is None:
        tokens = item.get("tokens") or []
        for token in tokens:
            if isinstance(token, dict) and str(token.get("outcome", "")).lower() == "yes":
                implied = float(token.get("price", 0.5))
                break
    return MarketMetadata(
        market_id=market_id,
        resolution_criteria=str(item.get("description") or item.get("resolution_criteria") or ""),
        category_tags=list(item.get("tags") or []),
        dispute_history=item.get("dispute_history"),
        implied_probability_yes=_clip01(float(implied if implied is not None else 0.5)),
    )


def _parse_resolution(market_id: str, raw: Any) -> Resolution | None:
    item = raw if isinstance(raw, dict) else {}
    if not item.get("closed") and item.get("outcome") is None:
        return None
    outcome_raw = item.get("outcome")
    if outcome_raw is None:
        return None
    outcome = bool(outcome_raw) if not isinstance(outcome_raw, str) else outcome_raw.lower() == "yes"
    return Resolution(
        market_id=market_id,
        outcome=outcome,
        resolved_at=_parse_dt(item.get("resolved_at") or item.get("end_date_iso")) or datetime.now(UTC),
        settlement_price=_clip01(float(item.get("settlement_price", 1.0 if outcome else 0.0))),
        disputed=bool(item.get("disputed", False)),
    )


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None


def _to_clob_order_args(order: Order) -> dict[str, Any]:
    return {
        "token_id": order.market_id,
        "price": float(order.price),
        "size": float(order.size),
        "side": "BUY" if order.side == "yes" else "SELL",
        "client_order_id": order.idempotency_key,
    }


def _parse_order_result(raw: Any) -> OrderResult:
    item = raw if isinstance(raw, dict) else {}
    status_text = str(item.get("status", "")).lower()
    status = _map_status(status_text)
    return OrderResult(
        status=status,
        fill_price=_optional_float(item.get("fill_price") or item.get("price")),
        filled_size=float(item.get("filled_size", item.get("size_matched", 0)) or 0),
        fees=float(item.get("fees", 0) or 0),
        broker_order_id=str(item["order_id"]) if item.get("order_id") else None,
    )


def _map_status(text: str) -> str:
    if text in {"matched", "filled", "live_filled"}:
        return "filled"
    if text in {"partial", "partially_filled"}:
        return "partial"
    if text in {"cancelled", "canceled"}:
        return "cancelled"
    if text in {"rejected", "error", "failed"}:
        return "rejected"
    return "open"


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_cancel_result(order_id: str, raw: Any) -> CancelResult:
    item = raw if isinstance(raw, dict) else {}
    if item.get("not_canceled"):
        return CancelResult(order_id=order_id, status="error", cancelled_size=0.0)
    cancelled_size = 0.0
    canceled = item.get("canceled") if isinstance(item.get("canceled"), list) else None
    if canceled and isinstance(canceled[0], dict):
        cancelled_size = float(canceled[0].get("size", 0) or 0)
    return CancelResult(order_id=order_id, status="cancelled", cancelled_size=cancelled_size)
