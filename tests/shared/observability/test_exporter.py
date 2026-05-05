"""HTTP-roundtrip tests for the Prometheus exporter daemon.

We use a fresh ``CollectorRegistry`` per test (instead of the global one
prometheus_client ships) and ``start_http_server`` against an ephemeral
port. This proves the wire format without any global-state pollution.
"""

from __future__ import annotations

import socket
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from prometheus_client import CollectorRegistry, start_http_server
from prometheus_client.parser import text_string_to_metric_families

from shared.config.settings import Settings
from shared.observability import exporter
from shared.observability.collector import CycleMetricsCollector


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _EmptyResult:
    def first(self) -> Any:
        return None

    def all(self) -> list[Any]:
        return []


class _EmptySession:
    """Session stub: every query returns no rows. Lets the collector emit
    each metric family with its zero/default value without touching a DB."""

    def execute(self, _query: object) -> _EmptyResult:
        return _EmptyResult()


@contextmanager
def _empty_ctx() -> Iterator[_EmptySession]:
    yield _EmptySession()


def test_disabled_metrics_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "false")
    rc = exporter.main(Settings())
    assert rc == 0


def test_metrics_endpoint_returns_text_format() -> None:
    port = _free_port()
    registry = CollectorRegistry()
    factory = cast("Any", _empty_ctx)
    registry.register(CycleMetricsCollector(session_factory=factory))

    server, server_thread = start_http_server(port=port, addr="127.0.0.1", registry=registry)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2.0) as resp:
            body = resp.read().decode("utf-8")
            assert resp.status == 200
    finally:
        server.shutdown()
        if isinstance(server_thread, threading.Thread):
            server_thread.join(timeout=2.0)

    families = {f.name for f in text_string_to_metric_families(body)}
    # All eight Spec metrics show up even with no DB rows (the collector
    # falls back to zero/default values, plus the scrape-errors counter).
    # Counter family names lose their "_total" suffix when the wire format is
    # parsed back — the parser strips the convention suffix. Gauges and the
    # histogram keep their full names.
    expected_subset = {
        "cycle_duration_seconds",
        "decisions",
        "trades",
        "errors",
        "equity_usd",
        "gross_exposure_usd",
        "drawdown_pct",
        "kill_switch_active",
        "metrics_scrape_errors",
    }
    assert expected_subset <= families
