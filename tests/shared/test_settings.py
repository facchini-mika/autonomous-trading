"""Smoke tests for Settings: defaults + env overrides."""

from __future__ import annotations

import os

import pytest

from shared.config.settings import Settings, reconciliation_flag_key


def test_defaults_are_paper_mode() -> None:
    settings = Settings()
    assert settings.TRADING_MODE == "paper"
    assert settings.PAPER_STARTING_CASH_USD == 10000.0
    assert settings.CONCENTRATION_CAP == 0.15
    assert settings.CYCLE_CAP == 0.25
    assert settings.EDGE_THRESHOLD == 0.03
    assert settings.FEE_RATE_BPS == 200


def test_universe_selection_threshold_defaults() -> None:
    settings = Settings()
    assert settings.MIN_DEPTH_1PCT_USD == 100.0
    assert settings.MAX_SPREAD == 0.10
    assert settings.MIN_TIME_TO_RESOLUTION_HOURS == 6
    assert settings.MAX_TIME_TO_RESOLUTION_DAYS == 30
    assert settings.SOON_RESOLVE_THRESHOLD_DAYS == 7
    assert settings.SOON_RESOLVE_BOOST_MULTIPLIER == 1.5


def test_real_capital_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "real_capital")
    settings = Settings()
    assert settings.TRADING_MODE == "real_capital"


def test_invalid_trading_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "live_yolo")
    with pytest.raises(ValueError, match="TRADING_MODE"):
        Settings()


def test_reconciliation_flag_key() -> None:
    assert reconciliation_flag_key("trade-123") == "reconciliation_flag:trade-123"


def test_extra_env_vars_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNRELATED_VAR", "ignored")
    settings = Settings()
    assert os.getenv("UNRELATED_VAR") == "ignored"
    assert not hasattr(settings, "UNRELATED_VAR")


def test_web_search_blocked_domains_default() -> None:
    assert Settings().WEB_SEARCH_BLOCKED_DOMAINS == ["coinmarketcap.com"]


def test_web_search_blocked_domains_csv_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_SEARCH_BLOCKED_DOMAINS", "a.com, b.com ,c.com")
    assert Settings().WEB_SEARCH_BLOCKED_DOMAINS == ["a.com", "b.com", "c.com"]


def test_web_search_blocked_domains_json_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_SEARCH_BLOCKED_DOMAINS", '["x.com","y.com"]')
    assert Settings().WEB_SEARCH_BLOCKED_DOMAINS == ["x.com", "y.com"]
