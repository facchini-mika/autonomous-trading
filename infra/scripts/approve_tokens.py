"""One-shot token approval for Polymarket trading (V1 + V2 paths).

Polymarket's CLOB v2 cutover (2026-04-28) introduced new exchange contracts
and a new collateral token (pUSD = `0xC011a7E…E82DFB`) that wraps USDC.e
1:1. The V2 exchanges (`0xE111…996B` and `0xe222…0F59`) only ever touch
the immutable `collateral` slot, which is pUSD. USDC.e is never spoken to
directly by V2 — users wrap it via `CollateralOnramp.wrap()` first.

Approvals set by this script (idempotent):

V1 (legacy, kept for any pre-migration cleanup orders) — still useful even
post-cutover so the wallet can settle V1 positions:
- USDC.e -> CTFExchange (V1)
- USDC.e -> NegRiskCtfExchange (V1)
- CTF    -> CTFExchange (V1)
- CTF    -> NegRiskCtfExchange (V1)

V2 (the path new orders use):
- USDC.e -> CollateralOnramp (so we can wrap USDC.e into pUSD)
- pUSD   -> CTFExchangeV2
- pUSD   -> NegRiskCtfExchangeV2
- CTF    -> CTFExchangeV2
- CTF    -> NegRiskCtfExchangeV2

After this script: run `infra/scripts/wrap_usdc.py` to convert the wallet's
USDC.e into pUSD (the V2 exchange's collateral). Without pUSD, the V2 order
posts will sign successfully but on-chain settlement will fail.

USAGE
-----
    uv run python -m infra.scripts.approve_tokens

Reads wallet from `Settings.KEY_PROVIDER_PATH`; passphrase via
`WALLET_PASSPHRASE` env or interactive prompt. Polygon RPC URL from
`Settings.POLYGON_RPC_URL` (env-overridable).
"""

from __future__ import annotations

import logging
from typing import Final, cast

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from web3.types import Nonce, TxParams

from shared.adapters.key_provider_localfile import KeyProviderLocalFile
from shared.config.settings import Settings

logger = logging.getLogger(__name__)

# Tokens (ERC20 + ERC1155).
USDC_ADDRESS: Final = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD_ADDRESS: Final = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_ADDRESS: Final = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# V1 (legacy) exchange addresses.
CTF_EXCHANGE_ADDRESS: Final = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE_ADDRESS: Final = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# V2 exchange addresses + collateral onramp (post-2026-04-28 cutover).
COLLATERAL_ONRAMP_ADDRESS: Final = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
CTF_EXCHANGE_V2_ADDRESS: Final = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2_ADDRESS: Final = "0xe2222d279d744050d28e00520010520000310F59"

ERC20_ABI: Final = [
    {
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
        "stateMutability": "nonpayable",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view",
    },
]
ERC1155_ABI: Final = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
        "stateMutability": "nonpayable",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
        "stateMutability": "view",
    },
]
MAX_UINT256: Final = (1 << 256) - 1


def main() -> None:
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

    nonce = int(w3.eth.get_transaction_count(address, "pending"))
    logger.info("Starting nonce (pending): %d", nonce)

    # V1 approvals (legacy contracts, kept for migration orders).
    nonce = _ensure_erc20_approval(
        w3,
        priv_key,
        address,
        USDC_ADDRESS,
        CTF_EXCHANGE_ADDRESS,
        "USDC -> CTF Exchange (V1)",
        nonce,
    )
    nonce = _ensure_erc20_approval(
        w3,
        priv_key,
        address,
        USDC_ADDRESS,
        NEG_RISK_EXCHANGE_ADDRESS,
        "USDC -> NegRisk Exchange (V1)",
        nonce,
    )
    nonce = _ensure_erc1155_approval(
        w3,
        priv_key,
        address,
        CTF_ADDRESS,
        CTF_EXCHANGE_ADDRESS,
        "CTF -> CTF Exchange (V1)",
        nonce,
    )
    nonce = _ensure_erc1155_approval(
        w3,
        priv_key,
        address,
        CTF_ADDRESS,
        NEG_RISK_EXCHANGE_ADDRESS,
        "CTF -> NegRisk Exchange (V1)",
        nonce,
    )

    # V2 approvals (current trading path).
    nonce = _ensure_erc20_approval(
        w3,
        priv_key,
        address,
        USDC_ADDRESS,
        COLLATERAL_ONRAMP_ADDRESS,
        "USDC -> CollateralOnramp (V2 wrap)",
        nonce,
    )
    nonce = _ensure_erc20_approval(
        w3,
        priv_key,
        address,
        PUSD_ADDRESS,
        CTF_EXCHANGE_V2_ADDRESS,
        "pUSD -> CTF Exchange (V2)",
        nonce,
    )
    nonce = _ensure_erc20_approval(
        w3,
        priv_key,
        address,
        PUSD_ADDRESS,
        NEG_RISK_EXCHANGE_V2_ADDRESS,
        "pUSD -> NegRisk Exchange (V2)",
        nonce,
    )
    nonce = _ensure_erc1155_approval(
        w3,
        priv_key,
        address,
        CTF_ADDRESS,
        CTF_EXCHANGE_V2_ADDRESS,
        "CTF -> CTF Exchange (V2)",
        nonce,
    )
    nonce = _ensure_erc1155_approval(
        w3,
        priv_key,
        address,
        CTF_ADDRESS,
        NEG_RISK_EXCHANGE_V2_ADDRESS,
        "CTF -> NegRisk Exchange (V2)",
        nonce,
    )

    logger.info("All V1 + V2 approvals confirmed on-chain for %s", address)
    logger.info("Next: run `uv run python -m infra.scripts.wrap_usdc` to mint pUSD")


def _ensure_erc20_approval(
    w3: Web3,
    priv_key: bytes,
    sender: str,
    token: str,
    spender: str,
    label: str,
    nonce: int,
) -> int:
    contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    current = contract.functions.allowance(sender, Web3.to_checksum_address(spender)).call()
    if current == MAX_UINT256:
        logger.info("[%s] already MAX_UINT256, skipping", label)
        return nonce
    tx = contract.functions.approve(Web3.to_checksum_address(spender), MAX_UINT256).build_transaction(
        cast("TxParams", {"from": sender, "nonce": Nonce(nonce)}),
    )
    _send_and_wait(w3, priv_key, tx, label)
    return nonce + 1


def _ensure_erc1155_approval(
    w3: Web3,
    priv_key: bytes,
    sender: str,
    token: str,
    operator: str,
    label: str,
    nonce: int,
) -> int:
    contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC1155_ABI)
    if contract.functions.isApprovedForAll(sender, Web3.to_checksum_address(operator)).call():
        logger.info("[%s] already isApprovedForAll, skipping", label)
        return nonce
    tx = contract.functions.setApprovalForAll(Web3.to_checksum_address(operator), True).build_transaction(  # noqa: FBT003
        cast("TxParams", {"from": sender, "nonce": Nonce(nonce)}),
    )
    _send_and_wait(w3, priv_key, tx, label)
    return nonce + 1


def _send_and_wait(w3: Web3, priv_key: bytes, tx: TxParams, label: str) -> None:
    signed = w3.eth.account.sign_transaction(tx, private_key=priv_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("[%s] tx: 0x%s", label, tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        msg = f"[{label}] transaction reverted (block {receipt['blockNumber']})"
        raise RuntimeError(msg)
    logger.info("[%s] confirmed in block %d", label, receipt["blockNumber"])


if __name__ == "__main__":
    main()
