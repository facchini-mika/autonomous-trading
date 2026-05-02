"""Tests for KeyProviderLocalFile (Fernet + EIP-712)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from shared.adapters.key_provider import KeyProvider
from shared.adapters.key_provider_localfile import (
    ENV_PASSPHRASE,
    KeyProviderLocalFile,
    WalletDecryptError,
    WalletNotInitialisedError,
)

# Hardhat default account #0 — well-known test vector.
HARDHAT_PRIV = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
HARDHAT_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


@pytest.fixture
def wallet_path(tmp_path: Path) -> Path:
    return tmp_path / "wallet.json"


def test_satisfies_protocol(wallet_path: Path) -> None:
    provider = KeyProviderLocalFile.create_new(
        wallet_path,
        private_key_hex=HARDHAT_PRIV,
        passphrase="x",
    )
    assert isinstance(provider, KeyProvider)


def test_create_new_writes_0600(wallet_path: Path) -> None:
    KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")
    mode = stat.S_IMODE(wallet_path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_create_new_refuses_overwrite(wallet_path: Path) -> None:
    KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")
    with pytest.raises(FileExistsError):
        KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")


def test_address_matches_known_priv_key(wallet_path: Path) -> None:
    provider = KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")
    assert provider.address().lower() == HARDHAT_ADDR.lower()


def test_round_trip_encrypt_decrypt(wallet_path: Path) -> None:
    KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="hunter2")
    fresh = KeyProviderLocalFile(path=wallet_path, passphrase_provider=lambda: "hunter2")
    assert fresh.address().lower() == HARDHAT_ADDR.lower()
    assert fresh._unsafe_export_priv_key().hex() == HARDHAT_PRIV.removeprefix("0x")


def test_wrong_passphrase_raises(wallet_path: Path) -> None:
    KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="correct")
    fresh = KeyProviderLocalFile(path=wallet_path, passphrase_provider=lambda: "wrong")
    with pytest.raises(WalletDecryptError):
        fresh.address()


def test_missing_file_raises(wallet_path: Path) -> None:
    fresh = KeyProviderLocalFile(path=wallet_path, passphrase_provider=lambda: "x")
    with pytest.raises(WalletNotInitialisedError):
        fresh.address()


def test_corrupted_file_raises(wallet_path: Path) -> None:
    KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")
    payload = json.loads(wallet_path.read_text())
    payload["ciphertext_b64"] = "not-base64"
    wallet_path.write_text(json.dumps(payload))
    fresh = KeyProviderLocalFile(path=wallet_path, passphrase_provider=lambda: "x")
    with pytest.raises(WalletDecryptError):
        fresh.address()


def test_passphrase_from_env_takes_precedence(wallet_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="env-secret")
    monkeypatch.setenv(ENV_PASSPHRASE, "env-secret")
    calls = {"n": 0}

    def fail() -> str:
        calls["n"] += 1
        msg = "should not be called"
        raise AssertionError(msg)

    fresh = KeyProviderLocalFile(path=wallet_path, passphrase_provider=fail)
    assert fresh.address().lower() == HARDHAT_ADDR.lower()
    assert calls["n"] == 0


def test_passphrase_provider_called_only_once(wallet_path: Path) -> None:
    KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")
    if ENV_PASSPHRASE in os.environ:
        del os.environ[ENV_PASSPHRASE]
    calls = {"n": 0}

    def prov() -> str:
        calls["n"] += 1
        return "x"

    fresh = KeyProviderLocalFile(path=wallet_path, passphrase_provider=prov)
    fresh.address()
    fresh.address()
    fresh._unsafe_export_priv_key()
    assert calls["n"] == 1


def test_eip712_signature_is_65_bytes(wallet_path: Path) -> None:
    provider = KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")
    typed = _sample_typed_data()
    sig = provider.sign_eip712(typed)
    assert isinstance(sig, bytes)
    assert len(sig) == 65


def test_eip712_roundtrip_recovers_address(wallet_path: Path) -> None:
    provider = KeyProviderLocalFile.create_new(wallet_path, private_key_hex=HARDHAT_PRIV, passphrase="x")
    typed = _sample_typed_data()
    sig = provider.sign_eip712(typed)
    msg = encode_typed_data(full_message=typed)
    recovered = Account.recover_message(msg, signature=sig)
    assert recovered.lower() == provider.address().lower()


def test_invalid_priv_key_length_rejected(wallet_path: Path) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        KeyProviderLocalFile.create_new(wallet_path, private_key_hex="0xdeadbeef", passphrase="x")


def _sample_typed_data() -> dict[str, object]:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": "1",
            "chainId": 137,
            "verifyingContract": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
        },
        "message": {
            "salt": 1234,
            "maker": HARDHAT_ADDR,
            "tokenId": 99,
            "makerAmount": 1_000_000,
            "takerAmount": 1_000_000,
            "side": 0,
        },
    }
