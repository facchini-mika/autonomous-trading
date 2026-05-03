"""Rotate the wallet passphrase (re-encrypt with same private key).

Decrypts `Settings.KEY_PROVIDER_PATH` with the old passphrase, then
re-encrypts it with a new passphrase. The private key, address, and
on-chain token approvals are unchanged. The original file is moved to a
`*.bak-<UTC-timestamp>` sibling for one-step rollback.

USAGE
-----
    uv run python -m infra.scripts.wallet_rotate_passphrase

Old passphrase via `OLD_WALLET_PASSPHRASE` env (single read) or interactive
`getpass` prompt. New passphrase via `WALLET_PASSPHRASE` env (single read,
no confirmation) or interactive `getpass` prompt with confirmation.
"""

from __future__ import annotations

import getpass
import logging
import os
import sys

from shared.adapters.key_provider_localfile import (
    ENV_PASSPHRASE,
    KeyProviderLocalFile,
)
from shared.config.settings import Settings

logger = logging.getLogger(__name__)

ENV_OLD_PASSPHRASE = "OLD_WALLET_PASSPHRASE"  # noqa: S105
MIN_PASSPHRASE_LEN = 12


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings()
    path = settings.KEY_PROVIDER_PATH

    if not path.exists():
        logger.error("Wallet does not exist at %s; nothing to rotate", path)
        return 1

    old_passphrase = _read_old_passphrase()
    new_passphrase = _read_new_passphrase()

    if new_passphrase == old_passphrase:
        logger.error("New passphrase must differ from old passphrase; aborting")
        return 1

    provider = KeyProviderLocalFile.rotate_passphrase(
        path=path,
        old_passphrase=old_passphrase,
        new_passphrase=new_passphrase,
    )

    logger.info("Passphrase rotated for wallet at %s", path)
    logger.info("Address (unchanged): %s", provider.address())
    logger.info("Original file backed up alongside as %s.bak-<UTC-timestamp>", path.name)
    logger.info("Verify with: ls -la %s*", path)
    return 0


def _read_old_passphrase() -> str:
    env = os.environ.get(ENV_OLD_PASSPHRASE)
    if env:
        logger.warning("Reading old passphrase from %s env", ENV_OLD_PASSPHRASE)
        return env
    return getpass.getpass("Old wallet passphrase: ")


def _read_new_passphrase() -> str:
    env = os.environ.get(ENV_PASSPHRASE)
    if env:
        logger.warning(
            "Reading new passphrase from %s env; no confirmation prompt will be shown",
            ENV_PASSPHRASE,
        )
        if len(env) < MIN_PASSPHRASE_LEN:
            logger.error("Passphrase must be at least %d characters; aborting", MIN_PASSPHRASE_LEN)
            sys.exit(1)
        return env
    first = getpass.getpass("New wallet passphrase: ")
    second = getpass.getpass("Confirm new passphrase: ")
    if first != second:
        logger.error("Passphrases do not match; aborting")
        sys.exit(1)
    if len(first) < MIN_PASSPHRASE_LEN:
        logger.error("Passphrase must be at least %d characters; aborting", MIN_PASSPHRASE_LEN)
        sys.exit(1)
    return first


if __name__ == "__main__":
    sys.exit(main())
