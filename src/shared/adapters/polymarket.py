"""PolymarketAdapter — sync REST wrapper around `py-clob-client-v2`.

All orders are submitted with TIF=FAK (Fill-And-Kill / IOC). Any
unfilled portion is cancelled immediately by Polymarket — no resting
orders, by design. See `specs/data_infrastructure.md §2`.

Phase-6a swap: Polymarket's CLOB v2 cutover (2026-04-28) made the v1
EIP-712 domain (version="1", legacy exchange addresses) reject every
order with `order_version_mismatch`. The v2 client signs against the new
exchange contracts (`0xE111180000d2663C0091e4f400237545B87B996B`,
`0xe2222d279d744050d28e00520010520000310F59`) with domain version="2"
and a different order struct (timestamp/metadata/builder fields, no
nonce/expiration/feeRateBps). This adapter targets the v2 client.

Phase-4 Modus 1: a paket-private hook on `KeyProviderLocalFile`
(`_unsafe_export_priv_key`) hands the raw private key to py-clob-client-v2
which performs EIP-712 signing internally. The KeyProvider Protocol stays
intact for KMS migration in Phase 7 — see
`docs/adr/0001-keyprovider-pyclobclient-mode-1.md`.

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


class IdempotencyConflictError(PolymarketAdapterError):
    """A prior attempt for the same ``idempotency_key`` is still in-flight.

    Raised by ``place_order`` when the persistent store already holds a
    row for the key without a terminal status — i.e. a previous process
    crashed between ``POST /order`` and the response write. The order is
    deliberately not re-submitted; operator inspects the orphan via the
    cycle-start log + ``orphan_order_attempts_total`` metric.
    """


class IdempotencyStore(Protocol):
    """Backing store for `idempotency_key -> OrderResult` cache.

    ``reserve`` is the dedup gate: implementations must guarantee that
    only the first caller for a given key returns ``True``. Stores that
    cannot survive a process crash (e.g. the in-memory default) should
    document the limitation; production wires the Postgres-backed
    ``DbIdempotencyStore``.
    """

    def get(self, key: str) -> OrderResult | None: ...
    def reserve(self, key: str, *, cycle_id: str, decision_id: str) -> bool: ...
    def put(
        self,
        key: str,
        value: OrderResult,
        *,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> None: ...


class InMemoryIdempotencyStore:
    """Dict-backed store, lifetime = process lifetime.

    Only safe for tests and paper-mode where a crash cannot cause a
    duplicate broker-side submission. Real-capital trading wires
    ``DbIdempotencyStore`` instead — see ``shared.adapters.factory``.
    """

    def __init__(self) -> None:
        self._cache: dict[str, OrderResult] = {}
        self._reserved: set[str] = set()

    def get(self, key: str) -> OrderResult | None:
        return self._cache.get(key)

    def reserve(self, key: str, *, cycle_id: str, decision_id: str) -> bool:
        del cycle_id, decision_id  # unused in the in-memory variant
        if key in self._reserved:
            return False
        self._reserved.add(key)
        return True

    def put(
        self,
        key: str,
        value: OrderResult,
        *,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> None:
        del error_class, error_message  # unused in the in-memory variant
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
        default_fee_rate_bps: int = 0,
    ) -> None:
        self._key_provider = key_provider
        self._host = host
        self._chain_id = chain_id
        self._idempotency = idempotency_store or InMemoryIdempotencyStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = self._make_client(client_factory)
        self._api_creds_set = False
        self._default_fee_rate_bps = default_fee_rate_bps
        # market_id (condition_id) → yes_token_id, populated lazily during
        # ``get_markets`` and read by ``get_orderbook`` so the latter does
        # not have to issue a second HTTP fetch per call.
        self._yes_token_cache: dict[str, str] = {}

    # --- Reads ---------------------------------------------------------------

    def get_markets(self, *, limit: int) -> list[Market]:
        """Return up to ``limit`` actively-tradeable markets from the CLOB.

        Uses ``get_sampling_markets`` rather than ``get_markets`` because the
        latter paginates the universe of all markets ever (oldest first), so
        the first 1000 entries are dominated by long-resolved historical
        markets with no orderbook. ``get_sampling_markets`` returns the
        rewards-incentivised active subset (currently 1000 markets/page),
        which by construction are open + accepting orders + have an
        orderbook. A defensive client-side filter strips any item where
        Polymarket has flagged it inactive between server snapshots.

        Also populates the yes-token cache so subsequent ``get_orderbook``
        calls can resolve ``market_id → yes_token_id`` without a second
        HTTP fetch.
        """
        raw = self._retry(self._client.get_sampling_markets, op="get_sampling_markets")
        markets = _parse_markets_response(raw, clock=self._clock)
        for m in markets:
            if m.yes_token_id is not None:
                self._yes_token_cache[m.market_id] = m.yes_token_id
        return markets[:limit]

    def get_orderbook(self, market_id: str) -> Orderbook:
        """Fetch the orderbook for a market.

        ``market_id`` is the Polymarket condition id, but the CLOB endpoint
        takes the binary-outcome *token id*. We resolve via the yes-token
        cache populated during ``get_markets``; on a cache miss we fetch
        the market record once to learn the token id (then cache it).
        """
        token_id = self._resolve_yes_token(market_id)
        raw = self._retry(lambda: self._client.get_order_book(token_id), op="get_order_book")
        return _parse_orderbook(market_id, raw, clock=self._clock)

    def _resolve_yes_token(self, market_id: str) -> str:
        cached = self._yes_token_cache.get(market_id)
        if cached is not None:
            return cached
        raw = self._retry(lambda: self._client.get_market(market_id), op="get_market")
        if not isinstance(raw, dict):
            msg = f"get_market({market_id!r}) returned non-dict payload"
            raise PolymarketPermanentError(msg)
        yes_id, _ = _extract_token_ids(raw)
        if yes_id is None:
            msg = f"market {market_id!r} has no yes-token in CLOB response"
            raise PolymarketPermanentError(msg)
        self._yes_token_cache[market_id] = yes_id
        return yes_id

    def get_metadata(self, market_id: str) -> MarketMetadata:
        raw = self._retry(lambda: self._client.get_market(market_id), op="get_market")
        return _parse_metadata(market_id, raw)

    def get_resolution(self, market_id: str) -> Resolution | None:
        raw = self._retry(lambda: self._client.get_market(market_id), op="get_market")
        return _parse_resolution(market_id, raw)

    def estimate_fee(self, order: Order) -> float:
        """Estimate the taker fee in USD for `order` from the market metadata.

        Reads ``fee_rate_bps`` (or the v2 alias ``feeRateBps``) from the
        market response if present and falls back to ``default_fee_rate_bps``
        otherwise. Network failure also falls back so the cycle does not
        abort on a flaky metadata call — the solvency gate then operates
        on a conservative Settings-driven default.
        """
        try:
            raw = self._retry(lambda: self._client.get_market(order.market_id), op="get_market")
        except Exception as exc:
            logger.warning("Fee estimate fetch failed for %s: %s", order.market_id, exc)
            rate_bps = self._default_fee_rate_bps
        else:
            rate_bps = _parse_fee_rate_bps(raw, default=self._default_fee_rate_bps)
        return float(order.notional_usd) * rate_bps / 10000.0

    # --- Writes --------------------------------------------------------------

    def place_order(self, order: Order) -> OrderResult:
        # Three-step dedup against the (now durable) idempotency store:
        # 1. cache-hit  → terminal result is already known, return it.
        # 2. reserve()  → first writer wins; a False return means a row
        #                 exists without `finished_at`, i.e. an earlier
        #                 process crashed mid-submit. We refuse to re-fire.
        # 3. put()      → finalise the row with the terminal status.
        cached = self._idempotency.get(order.idempotency_key)
        if cached is not None:
            logger.debug("Idempotency hit for key=%s; returning cached result", order.idempotency_key)
            return cached

        cycle_id, decision_id = _split_idempotency_key(order.idempotency_key)
        if not self._idempotency.reserve(order.idempotency_key, cycle_id=cycle_id, decision_id=decision_id):
            msg = (
                f"order_attempt {order.idempotency_key!r} is already in-flight "
                "or crashed mid-submit; refusing to re-fire"
            )
            logger.warning("idempotency_conflict key=%s", order.idempotency_key)
            raise IdempotencyConflictError(msg)

        self._ensure_api_creds()

        try:
            raw = self._retry(lambda: self._submit_order(order), op="post_order")
        except PolymarketPermanentError as exc:
            result = OrderResult(status="rejected", broker_order_id=None, fill_price=None, filled_size=0.0)
            logger.warning("Permanent error placing order %s: %s", order.idempotency_key, exc)
            self._idempotency.put(
                order.idempotency_key,
                result,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
            return result
        except PolymarketTransientError as exc:
            result = OrderResult(status="rejected", broker_order_id=None, fill_price=None, filled_size=0.0)
            logger.warning(
                "Transient error placing order %s after retries: %s",
                order.idempotency_key,
                exc,
            )
            self._idempotency.put(
                order.idempotency_key,
                result,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
            raise

        result = _parse_order_result(raw, order=order)
        self._idempotency.put(order.idempotency_key, result)
        return result

    def cancel_order(self, order_id: str) -> CancelResult:
        from py_clob_client_v2 import OrderPayload  # noqa: PLC0415

        self._ensure_api_creds()
        try:
            raw = self._retry(
                lambda: self._client.cancel_order(OrderPayload(orderID=order_id)),
                op="cancel_order",
            )
        except PolymarketPermanentError as exc:
            if _is_not_found(exc):
                return CancelResult(order_id=order_id, status="not_found", cancelled_size=0.0)
            return CancelResult(order_id=order_id, status="error", cancelled_size=0.0)
        return _parse_cancel_result(order_id, raw)

    # --- Internals -----------------------------------------------------------

    def _make_client(self, factory: Callable[..., Any] | None) -> Any:
        if factory is not None:
            return factory(host=self._host, chain_id=self._chain_id, key_provider=self._key_provider)
        from py_clob_client_v2 import ClobClient  # noqa: PLC0415

        from shared.adapters.key_provider_localfile import (  # noqa: PLC0415
            KeyProviderLocalFile,
        )

        if not isinstance(self._key_provider, KeyProviderLocalFile):
            msg = "PolymarketAdapter Modus 1 requires KeyProviderLocalFile (raw-key export)"
            raise TypeError(msg)
        priv_hex = self._key_provider._unsafe_export_priv_key().hex()  # noqa: SLF001
        return ClobClient(host=self._host, chain_id=self._chain_id, key=priv_hex)

    def _ensure_api_creds(self) -> None:
        if self._api_creds_set:
            return
        creds = self._client.create_or_derive_api_key()
        self._client.set_api_creds(creds)
        self._api_creds_set = True

    def _submit_order(self, order: Order) -> Any:
        from py_clob_client_v2 import OrderType  # noqa: PLC0415

        signed = self._client.create_order(_to_clob_order_args(order))
        return self._client.post_order(signed, order_type=OrderType.FAK)

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


def _split_idempotency_key(key: str) -> tuple[str, str]:
    """Split ``"<cycle_id>:<decision_id>"`` into its components.

    ``lead_bootstrap._decision_to_order`` constructs the key as
    ``f"{cycle_id}:{decision.id}"``. Decision ids are UUIDs (no colons);
    cycle ids are ``cycle-<unix-ts>`` (no colons either). A single split
    on the rightmost ``:`` is therefore lossless. We fall back to the
    full key on both sides if the format is unexpected (probe orders,
    test fixtures) so the store can still record the attempt.
    """
    if ":" not in key:
        return key, key
    cycle_id, decision_id = key.rsplit(":", 1)
    return cycle_id, decision_id


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
        if not _is_tradeable(item):
            continue
        try:
            out.append(_parse_market_item(item, clock=clock))
        except (KeyError, ValueError, TypeError):
            logger.debug("Skipping unparseable market item: %r", item)
    return out


def _is_tradeable(item: dict[str, Any]) -> bool:
    """Defensive filter: only keep markets that are open AND can take orders.

    Belt-and-suspenders alongside ``get_sampling_markets`` — covers the rare
    race where an entry is curated into the sampling set but Polymarket has
    flipped one of these flags before our snapshot lands.
    """
    if item.get("closed") is True:
        return False
    if item.get("accepting_orders") is False:
        return False
    return item.get("enable_order_book") is not False


def _parse_market_item(item: dict[str, Any], *, clock: Callable[[], datetime]) -> Market:
    yes_tok, no_tok = _extract_token_ids(item)
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
        yes_token_id=yes_tok,
        no_token_id=no_tok,
    )


def _extract_token_ids(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull (yes_token_id, no_token_id) from a CLOB market response.

    The ``tokens`` field is a list of ``{"outcome": "Yes"|"No", "token_id": "..."}``
    entries. Returns ``(None, None)`` for malformed payloads — the adapter
    will fall back to a ``get_market`` lookup when it needs the token.
    """
    tokens = item.get("tokens") or []
    if not isinstance(tokens, list):
        return None, None
    yes_id: str | None = None
    no_id: str | None = None
    for tok in tokens:
        if not isinstance(tok, dict):
            continue
        outcome = str(tok.get("outcome") or "").strip().lower()
        token_id = tok.get("token_id")
        if token_id is None:
            continue
        if outcome == "yes":
            yes_id = str(token_id)
        elif outcome == "no":
            no_id = str(token_id)
    return yes_id, no_id


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


def _parse_fee_rate_bps(raw: Any, *, default: int) -> int:
    """Pull a fee-rate-bps field from a market metadata response.

    Tries snake_case (``fee_rate_bps``) and v2 camelCase (``feeRateBps``).
    Returns ``default`` if neither is present or the value is unparseable.
    """
    if not isinstance(raw, dict):
        return default
    for key in ("fee_rate_bps", "feeRateBps"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


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


def _to_clob_order_args(order: Order) -> Any:
    """Build py-clob-client-v2 OrderArgs from our internal Order DTO.

    The v2 alias `OrderArgs == OrderArgsV2`; the v2 order struct dropped
    `nonce`/`expiration`/`feeRateBps`/`taker` and added timestamp/metadata/
    builder fields, all populated by the client at sign time. Our DTO only
    needs to carry token_id/price/size/side; the idempotency_key stays in
    the adapter-side cache (it is unrelated to the on-chain timestamp salt
    that v2 uses for replay protection).
    """
    from py_clob_client_v2 import OrderArgs  # noqa: PLC0415

    return OrderArgs(
        token_id=order.market_id,
        price=float(order.price),
        size=float(order.size),
        side="BUY" if order.side == "yes" else "SELL",
    )


def _parse_order_result(raw: Any, *, order: Order | None = None) -> OrderResult:
    """Parse a CLOB v2 `POST /order` response into our internal `OrderResult`.

    V2 response shape: `{orderID, takingAmount, makingAmount, status,
    transactionsHashes, errorMsg}`. `takingAmount`/`makingAmount` express the
    fill in absolute units of (asset received, asset paid); for a BUY the asset
    received is the conditional token (filled_size) and `makingAmount` is the
    pUSD spent. Fees aren't in the response — query trades to recover them.
    """
    item = raw if isinstance(raw, dict) else {}
    status_text = str(item.get("status", "")).lower()
    status = _map_status(status_text)

    broker_order_id = item.get("orderID") or item.get("order_id")
    taking = _optional_float(item.get("takingAmount") or item.get("taking_amount"))
    making = _optional_float(item.get("makingAmount") or item.get("making_amount"))

    side_is_buy = order is None or order.side == "yes"
    fill_price: float | None
    if taking is not None and making is not None and taking > 0 and making > 0:
        filled_size = taking if side_is_buy else making
        fill_price = (making / taking) if side_is_buy else (taking / making)
    else:
        # V1-shape fallback (legacy tests + any pre-migration response).
        filled_size = float(item.get("filled_size", item.get("size_matched", 0)) or 0)
        fill_price = _optional_float(item.get("fill_price") or item.get("price"))

    # Under FAK, Polymarket reports `status="matched"` for both full and partial
    # fills — the unfilled remainder is cancelled silently. Disambiguate against
    # the requested order size so downstream consumers see the right state.
    if order is not None and status == "filled":
        if float(filled_size) <= 0.0:
            status = "rejected"
        elif float(filled_size) < float(order.size):
            status = "partial"

    return OrderResult(
        status=status,
        fill_price=fill_price,
        filled_size=float(filled_size),
        fees=float(item.get("fees", 0) or 0),
        broker_order_id=str(broker_order_id) if broker_order_id else None,
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
