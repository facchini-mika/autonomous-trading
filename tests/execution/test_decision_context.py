"""Tests for the decision_id ContextVar."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest

from execution.decision_context import current_decision_id, require_decision_id, with_decision


def test_default_is_none() -> None:
    assert current_decision_id.get() is None


def test_with_decision_sets_and_resets() -> None:
    uid = uuid4()
    with with_decision(uid):
        assert current_decision_id.get() == uid
    assert current_decision_id.get() is None


def test_nested_with_decision_restores_outer() -> None:
    outer = uuid4()
    inner = uuid4()
    with with_decision(outer):
        assert current_decision_id.get() == outer
        with with_decision(inner):
            assert current_decision_id.get() == inner
        assert current_decision_id.get() == outer
    assert current_decision_id.get() is None


def test_require_decision_id_raises_outside() -> None:
    with pytest.raises(RuntimeError, match="No active decision_id"):
        require_decision_id()


def test_require_decision_id_returns_inside() -> None:
    uid = uuid4()
    with with_decision(uid):
        assert require_decision_id() == uid


def test_threads_dont_share_decision_id() -> None:
    seen: dict[str, object] = {}

    def worker() -> None:
        seen["thread"] = current_decision_id.get()

    with with_decision(uuid4()):
        t = threading.Thread(target=worker)
        t.start()
        t.join()

    assert seen["thread"] is None
