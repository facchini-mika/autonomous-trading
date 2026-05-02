"""Tests for Gamma client (httpx mocked via respx)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from execution.gamma_client import DEFAULT_HOST, GammaClient, GammaClientError


@pytest.fixture
def client() -> GammaClient:
    return GammaClient(host=DEFAULT_HOST, client=httpx.Client(timeout=5.0))


@respx.mock
def test_list_resolved_markets_yields_recent(client: GammaClient) -> None:
    now = datetime.now(UTC)
    older = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    newer = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    respx.get(f"{DEFAULT_HOST}/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"condition_id": "0xa", "end_date_iso": older, "closed": True, "outcome": True},
                {"condition_id": "0xb", "end_date_iso": newer, "closed": True, "outcome": False},
            ],
        ),
    )
    items = list(client.list_resolved_markets(since=now - timedelta(days=5)))
    assert len(items) == 1
    assert items[0]["condition_id"] == "0xb"


@respx.mock
def test_list_resolved_markets_paginates(client: GammaClient) -> None:
    now = datetime.now(UTC)
    recent = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    page1 = [{"condition_id": f"0x{i}", "end_date_iso": recent, "closed": True, "outcome": True} for i in range(100)]
    page2 = [{"condition_id": "0xlast", "end_date_iso": recent, "closed": True, "outcome": False}]
    route = respx.get(f"{DEFAULT_HOST}/markets")
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]
    items = list(client.list_resolved_markets(since=now - timedelta(days=1), page_size=100))
    assert len(items) == 101


@respx.mock
def test_get_resolution_returns_resolution(client: GammaClient) -> None:
    respx.get(f"{DEFAULT_HOST}/markets/0xabc").mock(
        return_value=httpx.Response(
            200,
            json={"closed": True, "outcome": True, "end_date_iso": "2026-04-30T00:00:00Z"},
        ),
    )
    res = client.get_resolution("0xabc")
    assert res is not None
    assert res.outcome is True


@respx.mock
def test_get_resolution_unresolved_returns_none(client: GammaClient) -> None:
    respx.get(f"{DEFAULT_HOST}/markets/0xnope").mock(return_value=httpx.Response(200, json={"closed": False}))
    assert client.get_resolution("0xnope") is None


@respx.mock
def test_get_resolution_uses_outcome_prices(client: GammaClient) -> None:
    respx.get(f"{DEFAULT_HOST}/markets/0xabc").mock(
        return_value=httpx.Response(
            200,
            json={"closed": True, "outcomePrices": ["1.0", "0.0"], "end_date_iso": "2026-04-30T00:00:00Z"},
        ),
    )
    res = client.get_resolution("0xabc")
    assert res is not None
    assert res.outcome is True


@respx.mock
def test_5xx_raises_gamma_error(client: GammaClient) -> None:
    respx.get(f"{DEFAULT_HOST}/markets/0xerr").mock(return_value=httpx.Response(500))
    with pytest.raises(GammaClientError):
        client.get_resolution("0xerr")


@respx.mock
def test_malformed_json_raises_gamma_error(client: GammaClient) -> None:
    respx.get(f"{DEFAULT_HOST}/markets/0xbad").mock(return_value=httpx.Response(200, text="not-json"))
    with pytest.raises(GammaClientError):
        client.get_resolution("0xbad")


@respx.mock
def test_network_error_raises_gamma_error(client: GammaClient) -> None:
    respx.get(f"{DEFAULT_HOST}/markets/0xnet").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(GammaClientError):
        client.get_resolution("0xnet")
