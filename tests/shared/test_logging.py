"""Tests for shared.logging — JSON output + §3 mandatory fields."""

from __future__ import annotations

import io
import json
import logging
import sys
from typing import Any

import pytest

from shared.logging import bind, clear, configure, get_logger, unbind


@pytest.fixture(autouse=True)
def _reset_contextvars() -> Any:
    clear()
    yield
    clear()


def _capture(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    return buf


def _last_json(buf: io.StringIO) -> dict[str, Any]:
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert lines, "no log line emitted"
    parsed = json.loads(lines[-1])
    assert isinstance(parsed, dict)
    return parsed


def test_emits_json_with_mandatory_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = _capture(monkeypatch)
    configure(service="trading_cycle")
    log = get_logger("x")
    log.info("cycle_starting")
    record = _last_json(buf)
    assert record["service"] == "trading_cycle"
    assert record["level"] == "info"
    assert record["event"] == "cycle_starting"
    assert "timestamp" in record


def test_contextvar_propagation(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = _capture(monkeypatch)
    configure(service="outcome_ingestion")
    bind(correlation_id="abc", cycle_id="cycle-1")
    log = get_logger("x")
    log.info("step", market_id="0xa")
    record = _last_json(buf)
    assert record["correlation_id"] == "abc"
    assert record["cycle_id"] == "cycle-1"
    assert record["market_id"] == "0xa"
    assert record["service"] == "outcome_ingestion"


def test_unbind_drops_contextvar(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = _capture(monkeypatch)
    configure(service="x")
    bind(cycle_id="c1")
    unbind("cycle_id")
    log = get_logger("x")
    log.info("after_unbind")
    record = _last_json(buf)
    assert "cycle_id" not in record


def test_clear_resets_contextvars(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = _capture(monkeypatch)
    configure(service="x")
    bind(cycle_id="c1", correlation_id="abc")
    clear()
    get_logger("x").info("after_clear")
    record = _last_json(buf)
    assert "cycle_id" not in record
    assert "correlation_id" not in record


def test_exception_renders_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = _capture(monkeypatch)
    configure(service="x")
    log = get_logger("x")

    def _raise() -> None:
        raise RuntimeError("boom")

    try:
        _raise()
    except RuntimeError:
        log.exception("caught")
    record = _last_json(buf)
    assert "exception" in record
    assert "RuntimeError: boom" in record["exception"]


def test_filtering_below_level(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = _capture(monkeypatch)
    configure(service="x", level=logging.WARNING)
    log = get_logger("x")
    log.info("hidden")
    log.warning("shown")
    record = _last_json(buf)
    assert record["event"] == "shown"
