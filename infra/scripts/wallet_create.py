"""One-shot encrypted-at-rest wallet creation for Polymarket trading.

Creates a new Fernet-encrypted wallet file at `Settings.KEY_PROVIDER_PATH`.
Refuses to overwrite an existing file. Either generates a fresh secp256k1
private key (default) or imports an existing one via `--import-hex`.

USAGE
-----
    uv run python -m infra.scripts.wallet_create                    # generate fresh
    uv run python -m infra.scripts.wallet_create --import-hex 0x... # import existing

Passphrase via `WALLET_PASSPHRASE` env (single read, no confirmation) or
interactive `getpass` prompt with confirmation.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import secrets
import sys

from eth_account import Account

from shared.adapters.key_provider_localfile import (
    ENV_PASSPHRASE,
    KeyProviderLocalFile,
)
from shared.config.settings import Settings

logger = logging.getLogger(__name__)

MIN_PASSPHRASE_LEN = 12


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    settings = Settings()
    path = settings.KEY_PROVIDER_PATH

    if path.exists():
        logger.error("Wallet already exists at %s; refusing to overwrite", path)
        return 1

    priv_key_hex = args.import_hex or _generate_priv_key_hex()
    passphrase = _read_passphrase()

    provider = KeyProviderLocalFile.create_new(
        path=path,
        private_key_hex=priv_key_hex,
        passphrase=passphrase,
    )
    address = provider.address()

    logger.info("Wallet written to %s (chmod 0600)", path)
    logger.info("Address: %s", address)
    if not args.import_hex:
        logger.info(
            "Fund this address on Polygon with USDC + MATIC before running approve_tokens.",
        )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--import-hex",
        metavar="HEX",
        help="Import an existing private key (64 hex chars, 0x prefix optional). "
        "Default: generate a fresh secp256k1 key.",
    )
    return parser.parse_args()


def _generate_priv_key_hex() -> str:
    raw = secrets.token_bytes(32)
    Account.from_key(raw)
    return "0x" + raw.hex()


def _read_passphrase() -> str:
    env = os.environ.get(ENV_PASSPHRASE)
    if env:
        logger.warning(
            "Reading passphrase from %s env; no confirmation prompt will be shown",
            ENV_PASSPHRASE,
        )
        return env
    first = getpass.getpass("Wallet passphrase: ")
    second = getpass.getpass("Confirm passphrase: ")
    if first != second:
        logger.error("Passphrases do not match; aborting")
        sys.exit(1)
    if len(first) < MIN_PASSPHRASE_LEN:
        logger.error("Passphrase must be at least %d characters; aborting", MIN_PASSPHRASE_LEN)
        sys.exit(1)
    return first


if __name__ == "__main__":
    sys.exit(main())
