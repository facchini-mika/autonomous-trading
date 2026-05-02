"""One-shot token approval for Polymarket trading.

Run once per wallet to approve USDC and Conditional Tokens (CTF) for the
Polymarket CTF Exchange. After this script completes successfully, the
adapter can place orders without per-call approvals.

USAGE
-----
    uv run python -m infra.scripts.approve_tokens

Reads wallet from `Settings.KEY_PROVIDER_PATH`; passphrase via
`WALLET_PASSPHRASE` env or interactive prompt.
"""

from __future__ import annotations

import logging
from typing import Final

from web3 import Web3

from shared.adapters.key_provider_localfile import KeyProviderLocalFile
from shared.config.settings import Settings

logger = logging.getLogger(__name__)

POLYGON_RPC: Final = "https://polygon-rpc.com"
USDC_ADDRESS: Final = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS: Final = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE_ADDRESS: Final = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE_ADDRESS: Final = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]
ERC1155_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function",
    },
]
MAX_UINT256 = (1 << 256) - 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    provider = KeyProviderLocalFile(path=settings.KEY_PROVIDER_PATH)
    address = provider.address()
    priv_key = provider._unsafe_export_priv_key()  # noqa: SLF001

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    if not w3.is_connected():
        msg = f"Polygon RPC unreachable: {POLYGON_RPC}"
        raise RuntimeError(msg)

    logger.info("Approving USDC -> CTF Exchange (%s)", CTF_EXCHANGE_ADDRESS)
    _approve_erc20(w3, priv_key, address, USDC_ADDRESS, CTF_EXCHANGE_ADDRESS)

    logger.info("Approving USDC -> NegRisk Exchange (%s)", NEG_RISK_EXCHANGE_ADDRESS)
    _approve_erc20(w3, priv_key, address, USDC_ADDRESS, NEG_RISK_EXCHANGE_ADDRESS)

    logger.info("Setting CTF approval-for-all -> CTF Exchange")
    _set_approval_for_all(w3, priv_key, address, CTF_ADDRESS, CTF_EXCHANGE_ADDRESS)

    logger.info("Setting CTF approval-for-all -> NegRisk Exchange")
    _set_approval_for_all(w3, priv_key, address, CTF_ADDRESS, NEG_RISK_EXCHANGE_ADDRESS)

    logger.info("All approvals submitted; verify via block explorer for %s", address)


def _approve_erc20(w3: Web3, priv_key: bytes, sender: str, token: str, spender: str) -> None:
    contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    tx = contract.functions.approve(Web3.to_checksum_address(spender), MAX_UINT256).build_transaction(
        {
            "from": Web3.to_checksum_address(sender),
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(sender)),
        },
    )
    signed = w3.eth.account.sign_transaction(tx, private_key=priv_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("approve tx: %s", tx_hash.hex())
    w3.eth.wait_for_transaction_receipt(tx_hash)


def _set_approval_for_all(w3: Web3, priv_key: bytes, sender: str, token: str, operator: str) -> None:
    contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC1155_ABI)
    tx = contract.functions.setApprovalForAll(Web3.to_checksum_address(operator), True).build_transaction(  # noqa: FBT003
        {
            "from": Web3.to_checksum_address(sender),
            "nonce": w3.eth.get_transaction_count(Web3.to_checksum_address(sender)),
        },
    )
    signed = w3.eth.account.sign_transaction(tx, private_key=priv_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    logger.info("setApprovalForAll tx: %s", tx_hash.hex())
    w3.eth.wait_for_transaction_receipt(tx_hash)


if __name__ == "__main__":
    main()
