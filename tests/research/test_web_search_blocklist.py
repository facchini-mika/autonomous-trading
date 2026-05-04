"""Unit tests for the web_search domain blocklist filter."""

from __future__ import annotations

import pytest

from research.skills.web_search import _is_blocked

_CMC = ["coinmarketcap.com"]


@pytest.mark.parametrize(
    "url",
    [
        "https://coinmarketcap.com/btc",
        "https://pro.coinmarketcap.com/api",
        "https://CoinMarketCap.COM/page",
    ],
)
def test_is_blocked_matches_host_and_subdomains(url: str) -> None:
    assert _is_blocked(url, _CMC) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://coingecko.com/btc",
        "https://notcoinmarketcap.com/x",
        "https://example.com",
    ],
)
def test_is_blocked_does_not_match_other_hosts(url: str) -> None:
    assert _is_blocked(url, _CMC) is False


def test_is_blocked_empty_blocklist_returns_false() -> None:
    assert _is_blocked("https://coinmarketcap.com", []) is False


def test_is_blocked_empty_url_returns_false() -> None:
    assert _is_blocked("", _CMC) is False


def test_is_blocked_malformed_url_returns_false() -> None:
    assert _is_blocked("not-a-url", _CMC) is False


def test_is_blocked_multiple_domains() -> None:
    blocked = ["coinmarketcap.com", "tracker.example.org"]
    assert _is_blocked("https://api.tracker.example.org/x", blocked) is True
    assert _is_blocked("https://safe.example.org/x", blocked) is False
