"""Phase-6a sandbox-smoke: place + verify a single $5 marketable-limit order.

Single-shot end-to-end test of the EIP-712 v2 signing roundtrip against the
live Polymarket CLOB v2. See `docs/operations/sandbox_smoke.md` for context.

USAGE
-----
    WALLET_PASSPHRASE='...' uv run python -m infra.scripts.sandbox_smoke

The market, side, size, and price are hardcoded for the operator-selected
target. `TRADING_MODE='real_capital'` forces the factory to pick the live
PolymarketAdapter; this bypasses the trading-cycle's risk gates by design
(direct adapter call, MAX_CAPITAL_EUR is not consulted).
"""

from __future__ import annotations

import logging
import sys

# Cloudflare TLS-fingerprints (JA3/JA4) py-clob-client-v2's httpx client and
# 403s the POST /auth/api-key. Replace the http client with curl_cffi
# impersonating Chrome -- this defeats both the UA filter and the TLS
# fingerprint check. Known issue: aggressive POST retries from the same IP
# still trip CF's IP-rep heuristics; expect a 30-60 min cooldown after
# several failed attempts.
from typing import Any

import py_clob_client_v2.http_helpers.helpers as _pchh
from curl_cffi import requests as _cffi_requests


class _ChromeImpersonatingClient:
    def __init__(self) -> None:
        self._session: Any = _cffi_requests.Session(impersonate="chrome136")

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if headers is not None:
            kwargs["headers"] = headers
        if content is not None:
            kwargs["data"] = content
        if json is not None:
            kwargs["json"] = json
        if params is not None:
            kwargs["params"] = params
        sys.stderr.write(f"DIAG > {method} {url}\n")
        resp = self._session.request(method, url, **kwargs)
        sys.stderr.write(f"DIAG < status={resp.status_code} body[:200]={resp.text[:200]!r}\n")
        return resp


_pchh._http_client = _ChromeImpersonatingClient()  # noqa: SLF001

from shared.adapters.factory import make_adapter  # noqa: E402
from shared.config.settings import Settings  # noqa: E402
from shared.models import Order  # noqa: E402

logger = logging.getLogger(__name__)

# Operator-selected target market: update token_id + condition_id per run.
# `YES_TOKEN_ID` is the asset_id (uint256 string) for the YES outcome
# (here "Up" since the market is a Bitcoin Up-or-Down).
# `CONDITION_ID` is the bytes32 hex of the underlying market condition.
# Current target: "Bitcoin Up or Down - May 3, 12:15PM-12:20PM ET", resolves
# 2026-05-03T16:20:00Z. Non-neg-risk -> uses CTFExchangeV2 directly.
YES_TOKEN_ID = "30135394038118131635864611711396616766056594420041600132666208362352258144884"  # noqa: S105
CONDITION_ID = "0x051ec4f9ba9fd59a2ea5d9f8259519cd411773eddeb5f10a6c26fde1f127ab68"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings(TRADING_MODE="real_capital")
    adapter = make_adapter(settings)

    order = Order(
        market_id=YES_TOKEN_ID,
        side="yes",
        size=5.0,
        price=0.60,
        notional_usd=3.10,
        idempotency_key="phase6a-smoke-002",
    )

    logger.info("Placing marketable-limit YES@0.60 size=5 on condition %s", CONDITION_ID)
    result = adapter.place_order(order)
    logger.info("OrderResult: %s", result)
    return 0 if result.status in {"filled", "partial", "open"} else 1


if __name__ == "__main__":
    sys.exit(main())
