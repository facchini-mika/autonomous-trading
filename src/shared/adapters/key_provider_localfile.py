"""Encrypted-at-rest local-file KeyProvider.

Stores a single signing key encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
on disk. The encryption key is derived from a passphrase via PBKDF2-HMAC-SHA256
(480 000 iterations) with a per-file 16-byte random salt.

The passphrase is read from the environment (`WALLET_PASSPHRASE`) when set,
otherwise from a `getpass.getpass()` prompt at first use. The decrypted private
key is cached only in memory and never written to disk or logged.

`_unsafe_export_priv_key` is a paket-private escape hatch consumed exclusively
by `polymarket.PolymarketAdapter` to hand the raw key to `py-clob-client` which
performs EIP-712 signing internally (Phase 4 Modus 1, see ADR 0001). The KMS
provider will deliberately raise `NotImplementedError` for this method, forcing
a Modus-2 (custom signer) refactor in Phase 7.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from eth_account import Account
from eth_account.messages import encode_typed_data

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS: Final = 480_000
SALT_BYTES: Final = 16
FILE_VERSION: Final = 1
PRIV_KEY_BYTES: Final = 32
ENV_PASSPHRASE: Final = "WALLET_PASSPHRASE"  # noqa: S105


class WalletNotInitialisedError(RuntimeError):
    """The wallet file does not exist or is unreadable."""


class WalletDecryptError(RuntimeError):
    """Passphrase is wrong or the file is corrupted."""


class KeyProviderLocalFile:
    """KeyProvider backed by a Fernet-encrypted JSON file."""

    def __init__(
        self,
        *,
        path: Path,
        passphrase_provider: Any = None,
    ) -> None:
        self._path = path
        self._passphrase_provider = passphrase_provider or _default_passphrase_provider
        self._priv_key: bytes | None = None
        self._address: str | None = None

    @classmethod
    def create_new(
        cls,
        path: Path,
        *,
        private_key_hex: str,
        passphrase: str,
    ) -> KeyProviderLocalFile:
        """Create a new wallet file from a raw private key + passphrase.

        Writes 0600 permissions; refuses to overwrite an existing file.
        """
        if path.exists():
            msg = f"Wallet already exists at {path}; refusing to overwrite"
            raise FileExistsError(msg)
        path.parent.mkdir(parents=True, exist_ok=True)

        priv_key_bytes = _hex_to_bytes(private_key_hex)
        address = Account.from_key(priv_key_bytes).address

        salt = secrets.token_bytes(SALT_BYTES)
        cipher = Fernet(_derive_key(passphrase, salt))
        ciphertext = cipher.encrypt(priv_key_bytes)

        payload = {
            "version": FILE_VERSION,
            "salt_b64": base64.b64encode(salt).decode("ascii"),
            "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
            "address": address,
            "created_at": datetime.now(UTC).isoformat(),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        instance = cls(path=path, passphrase_provider=lambda: passphrase)
        instance._priv_key = priv_key_bytes
        instance._address = address
        return instance

    def address(self) -> str:
        if self._address is None:
            self._unlock()
        if self._address is None:
            msg = "Wallet did not yield an address"
            raise WalletDecryptError(msg)
        return self._address

    def sign_eip712(self, typed_data: dict[str, Any]) -> bytes:
        priv = self._priv_key_or_raise()
        signable = encode_typed_data(full_message=typed_data)
        signed = Account.sign_message(signable, private_key=priv)
        return bytes(signed.signature)

    def _unsafe_export_priv_key(self) -> bytes:
        """Return the raw 32-byte private key (paket-private).

        Consumed only by `PolymarketAdapter` for py-clob-client Modus-1.
        See ADR 0001 for the Phase-7 migration path away from this method.
        """
        return self._priv_key_or_raise()

    def _priv_key_or_raise(self) -> bytes:
        self._unlock()
        if self._priv_key is None:
            msg = "Wallet did not yield a private key"
            raise WalletDecryptError(msg)
        return self._priv_key

    def _unlock(self) -> None:
        if self._priv_key is not None:
            return
        if not self._path.exists():
            msg = f"Wallet file not found at {self._path}"
            raise WalletNotInitialisedError(msg)

        passphrase = self._read_passphrase()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"Cannot read wallet file at {self._path}"
            raise WalletDecryptError(msg) from exc

        try:
            salt = base64.b64decode(payload["salt_b64"])
            ciphertext = base64.b64decode(payload["ciphertext_b64"])
            address = payload["address"]
        except (KeyError, ValueError) as exc:
            msg = "Wallet file is corrupted or missing required fields"
            raise WalletDecryptError(msg) from exc

        cipher = Fernet(_derive_key(passphrase, salt))
        try:
            priv_key = cipher.decrypt(ciphertext)
        except InvalidToken as exc:
            msg = "Wallet decrypt failed: wrong passphrase or corrupted file"
            raise WalletDecryptError(msg) from exc

        derived_address = Account.from_key(priv_key).address
        if derived_address.lower() != str(address).lower():
            msg = "Wallet address mismatch between stored payload and decrypted key"
            raise WalletDecryptError(msg)

        self._priv_key = priv_key
        self._address = derived_address

    def _read_passphrase(self) -> str:
        env = os.environ.get(ENV_PASSPHRASE)
        if env:
            logger.warning("Reading wallet passphrase from %s; this is insecure on shared hosts", ENV_PASSPHRASE)
            return env
        return str(self._passphrase_provider())


def _default_passphrase_provider() -> str:
    return getpass.getpass("Wallet passphrase: ")


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _hex_to_bytes(hex_str: str) -> bytes:
    cleaned = hex_str.removeprefix("0x")
    out = bytes.fromhex(cleaned)
    if len(out) != PRIV_KEY_BYTES:
        msg = f"Private key must be 32 bytes (64 hex chars), got {len(out)}"
        raise ValueError(msg)
    return out
