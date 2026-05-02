"""Tests for the adapter factory."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from shared.adapters.factory import make_adapter, make_key_provider
from shared.adapters.key_provider import KeyProvider
from shared.adapters.key_provider_localfile import KeyProviderLocalFile
from shared.adapters.paper_trading import PaperTradingAdapter
from shared.adapters.polymarket import PolymarketAdapter
from shared.config.settings import Settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def wallet(tmp_path: Path) -> Path:
    path: Path = tmp_path / "wallet.json"
    KeyProviderLocalFile.create_new(
        path,
        private_key_hex="0x" + "a" * 64,
        passphrase="x",
    )
    return path


def test_make_key_provider_returns_localfile(wallet) -> None:
    settings = Settings(KEY_PROVIDER_PATH=wallet, KEY_PROVIDER="encrypted_file")
    provider = make_key_provider(settings)
    assert isinstance(provider, KeyProviderLocalFile)
    assert isinstance(provider, KeyProvider)


def test_make_key_provider_kms_raises_not_implemented() -> None:
    settings = Settings(KEY_PROVIDER="aws_kms")
    with pytest.raises(NotImplementedError):
        make_key_provider(settings)


@contextmanager
def _dummy_session():
    yield MagicMock()


def test_default_trading_mode_is_paper_returns_paper_adapter(wallet, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLET_PASSPHRASE", "x")
    settings = Settings(KEY_PROVIDER_PATH=wallet)
    with patch("shared.adapters.factory.PolymarketAdapter") as mock_poly:
        mock_poly.return_value = MagicMock()
        adapter = make_adapter(
            settings,
            session_factory=_dummy_session,
            decision_id_provider=lambda: uuid4(),
        )
    assert isinstance(adapter, PaperTradingAdapter)


def test_real_capital_returns_polymarket_adapter(wallet, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLET_PASSPHRASE", "x")
    settings = Settings(TRADING_MODE="real_capital", KEY_PROVIDER_PATH=wallet)
    with patch("shared.adapters.factory.PolymarketAdapter") as mock_poly:
        instance = MagicMock(spec=PolymarketAdapter)
        mock_poly.return_value = instance
        adapter = make_adapter(settings)
    assert adapter is instance


def test_unknown_trading_mode_raises(wallet, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WALLET_PASSPHRASE", "x")
    settings = Settings(KEY_PROVIDER_PATH=wallet)
    object.__setattr__(settings, "TRADING_MODE", "unknown")
    with patch("shared.adapters.factory.PolymarketAdapter"), pytest.raises(ValueError, match="Unknown TRADING_MODE"):
        make_adapter(settings)
