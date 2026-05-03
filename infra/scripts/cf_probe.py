"""Cloudflare diagnostic probe for Polymarket /auth/api-key.

Tests REAL signed headers (using the wallet at KEY_PROVIDER_PATH) against
multiple curl_cffi impersonation profiles. Tells us whether CF blocks based
on TLS fingerprint, the signed-payload pattern, or wallet/IP combination.

USAGE
-----
    WALLET_PASSPHRASE='...' uv run python -m infra.scripts.cf_probe
"""
# ruff: noqa: T201

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from curl_cffi import requests as cffi_requests
from py_clob_client_v2.headers.headers import create_level_1_headers
from py_clob_client_v2.signer import Signer

from shared.adapters.key_provider_localfile import KeyProviderLocalFile
from shared.config.settings import Settings

logger = logging.getLogger(__name__)

IMPERSONATIONS = ["chrome131", "chrome136", "chrome142", "chrome146", "safari260", "firefox144"]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    provider = KeyProviderLocalFile(path=settings.KEY_PROVIDER_PATH)
    priv_hex = provider._unsafe_export_priv_key().hex()  # noqa: SLF001
    addr = provider.address()

    signer = Signer(private_key=priv_hex, chain_id=settings.POLYGON_CHAIN_ID)
    endpoint = f"{settings.POLYMARKET_HOST}/auth/api-key"

    print(f"# Wallet: {addr}", file=sys.stderr)
    print(f"# Endpoint: {endpoint}", file=sys.stderr)
    print(f"# Impersonations to test: {IMPERSONATIONS}", file=sys.stderr)
    print(file=sys.stderr)

    for imp in IMPERSONATIONS:
        # Build fresh real-signed headers per impersonation (timestamps differ)
        nonce = 0
        headers: dict[str, Any] = create_level_1_headers(signer, nonce)
        try:
            session: Any = cffi_requests.Session(impersonate=imp)  # type: ignore[arg-type]
            resp = session.post(endpoint, headers=headers, timeout=15)
            body_snip = resp.text[:80].replace("\n", " ")
            verdict = "CF-BLOCK" if "<!DOCTYPE html>" in resp.text[:60] else "PASSES-CF"
            print(f"{imp:12s} | status={resp.status_code} | {verdict:10s} | body[:80]={body_snip!r}")
        except Exception as exc:
            print(f"{imp:12s} | EXCEPTION: {type(exc).__name__}: {str(exc)[:80]}")
        # small jitter to avoid CF rate-limit confusing the diagnostic
        time.sleep(2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
