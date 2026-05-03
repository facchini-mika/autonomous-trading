"""Wrap USDC.e -> pUSD via Polymarket's V2 CollateralOnramp.

Polymarket's V2 CTF Exchange settles in pUSD (`0xC011…E82DFB`), a 1:1
wrapped USDC token. USDC.e and native USDC are convertible via
`CollateralOnramp.wrap(asset, to, amount)`. This script wraps the wallet's
entire USDC.e balance (or `--amount` if provided, in 6-decimal base units).

Pre-conditions:
- USDC.e -> CollateralOnramp allowance set (run `approve_tokens.py` first).
- Onramp not paused for USDC.e (`paused(USDC.e)` returns False — checked here).

Idempotent in spirit: skips when wallet has 0 USDC.e to wrap. Re-runnable.

USAGE
-----
    uv run python -m infra.scripts.wrap_usdc                # wrap full balance
    uv run python -m infra.scripts.wrap_usdc --amount 5_000_000  # wrap 5 USDC.e
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Final, cast

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.types import Nonce, TxParams

from shared.adapters.key_provider_localfile import KeyProviderLocalFile
from shared.config.settings import Settings

logger = logging.getLogger(__name__)

USDC_ADDRESS: Final = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD_ADDRESS: Final = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
COLLATERAL_ONRAMP_ADDRESS: Final = "0x93070a847efEf7F70739046A929D47a521F5B8ee"

ERC20_BALANCE_ABI: Final = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
]
ONRAMP_ABI: Final = [
    {
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_to", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "name": "wrap",
        "outputs": [],
        "type": "function",
        "stateMutability": "nonpayable",
    },
    {
        "inputs": [{"name": "asset", "type": "address"}],
        "name": "paused",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
        "stateMutability": "view",
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Wrap USDC.e -> pUSD via CollateralOnramp")
    parser.add_argument(
        "--amount",
        type=int,
        default=None,
        help="Amount in 6-decimal base units (e.g. 5_000_000 = 5 USDC.e). Default: full balance.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    provider = KeyProviderLocalFile(path=settings.KEY_PROVIDER_PATH)
    address = Web3.to_checksum_address(provider.address())
    priv_key = provider._unsafe_export_priv_key()  # noqa: SLF001

    w3 = Web3(Web3.HTTPProvider(settings.POLYGON_RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        msg = f"Polygon RPC unreachable: {settings.POLYGON_RPC_URL}"
        raise RuntimeError(msg)

    onramp = w3.eth.contract(address=Web3.to_checksum_address(COLLATERAL_ONRAMP_ADDRESS), abi=ONRAMP_ABI)
    if onramp.functions.paused(Web3.to_checksum_address(USDC_ADDRESS)).call():
        logger.error("CollateralOnramp is paused for USDC.e — cannot wrap")
        return 2

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_BALANCE_ABI)
    pusd = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=ERC20_BALANCE_ABI)
    usdc_before = int(usdc.functions.balanceOf(address).call())
    pusd_before = int(pusd.functions.balanceOf(address).call())
    logger.info("USDC.e balance: %s (= %.6f)", usdc_before, usdc_before / 1e6)
    logger.info("pUSD balance:   %s (= %.6f)", pusd_before, pusd_before / 1e6)

    amount = args.amount if args.amount is not None else usdc_before
    if amount <= 0:
        logger.info("Nothing to wrap (USDC.e balance is 0). Done.")
        return 0
    if amount > usdc_before:
        logger.error("Requested %s exceeds USDC.e balance %s", amount, usdc_before)
        return 2

    logger.info("Wrapping %s USDC.e (= %.6f) -> pUSD via Onramp at %s", amount, amount / 1e6, COLLATERAL_ONRAMP_ADDRESS)

    nonce = int(w3.eth.get_transaction_count(address, "pending"))
    tx = onramp.functions.wrap(
        Web3.to_checksum_address(USDC_ADDRESS),
        address,
        amount,
    ).build_transaction(cast("TxParams", {"from": address, "nonce": Nonce(nonce)}))

    signed = w3.eth.account.sign_transaction(tx, private_key=priv_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("wrap() tx: 0x%s", tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        msg = f"wrap reverted (block {receipt['blockNumber']})"
        raise RuntimeError(msg)
    logger.info("Confirmed in block %d", receipt["blockNumber"])

    usdc_after = int(usdc.functions.balanceOf(address).call())
    pusd_after = int(pusd.functions.balanceOf(address).call())
    logger.info("USDC.e: %s -> %s  (delta %s)", usdc_before, usdc_after, usdc_after - usdc_before)
    logger.info("pUSD:   %s -> %s  (delta %s)", pusd_before, pusd_after, pusd_after - pusd_before)
    return 0


if __name__ == "__main__":
    sys.exit(main())
