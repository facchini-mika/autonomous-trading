"""Tests for the notes_tool LRU manager."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from execution.notes_tool import DEFAULT_MAX_NOTES, manage_notes


@contextmanager
def _factory(session: MagicMock) -> Iterator[MagicMock]:
    yield session


def test_write_inserts_when_under_cap() -> None:
    sess = MagicMock()
    count = MagicMock()
    count.first.return_value = (5,)
    sess.execute.side_effect = [count, MagicMock()]

    out = manage_notes(
        action="write",
        agent_id="trading-agent",
        body="watch market X",
        tags=["x"],
        session_factory=lambda: _factory(sess),
        clock=lambda: datetime(2026, 5, 2, tzinfo=UTC),
    )

    assert len(out) == 1
    inserts = [c for c in sess.execute.call_args_list if "INSERT INTO notes" in str(c.args[0])]
    assert len(inserts) == 1


def test_write_evicts_when_at_cap() -> None:
    sess = MagicMock()
    count = MagicMock()
    count.first.return_value = (DEFAULT_MAX_NOTES,)
    sess.execute.side_effect = [count, MagicMock(), MagicMock()]

    manage_notes(
        action="write",
        agent_id="trading-agent",
        body="new note",
        session_factory=lambda: _factory(sess),
        clock=lambda: datetime(2026, 5, 2, tzinfo=UTC),
    )

    deletes = [c for c in sess.execute.call_args_list if "DELETE FROM notes" in str(c.args[0])]
    assert len(deletes) == 1
    sql = str(deletes[0].args[0])
    assert "ORDER BY last_accessed ASC" in sql


def test_read_returns_rows_and_updates_last_accessed() -> None:
    sess = MagicMock()
    selected = MagicMock()
    note_id = uuid4()
    selected.all.return_value = [
        SimpleNamespace(
            id=note_id,
            body="hello",
            tags=["t"],
            last_accessed=datetime(2026, 5, 1, tzinfo=UTC),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        ),
    ]
    sess.execute.side_effect = [selected, MagicMock()]

    out = manage_notes(
        action="read",
        agent_id="trading-agent",
        session_factory=lambda: _factory(sess),
    )
    assert len(out) == 1
    assert out[0]["body"] == "hello"
    update_calls = [c for c in sess.execute.call_args_list if "UPDATE notes" in str(c.args[0])]
    assert len(update_calls) == 1


def test_read_empty_does_not_run_update() -> None:
    sess = MagicMock()
    selected = MagicMock()
    selected.all.return_value = []
    sess.execute.return_value = selected

    out = manage_notes(
        action="read",
        agent_id="trading-agent",
        session_factory=lambda: _factory(sess),
    )
    assert out == []
    update_calls = [c for c in sess.execute.call_args_list if "UPDATE notes" in str(c.args[0])]
    assert len(update_calls) == 0


def test_edit_updates_existing() -> None:
    sess = MagicMock()
    sess.execute.return_value = MagicMock()
    note_id = uuid4()

    manage_notes(
        action="edit",
        agent_id="trading-agent",
        note_id=note_id,
        body="edited",
        tags=["new"],
        session_factory=lambda: _factory(sess),
    )
    update_calls = [c for c in sess.execute.call_args_list if "UPDATE notes" in str(c.args[0])]
    assert len(update_calls) == 1
    sql = str(update_calls[0].args[0])
    assert "tags" in sql


def test_edit_without_tags_omits_tags_update() -> None:
    sess = MagicMock()
    sess.execute.return_value = MagicMock()

    manage_notes(
        action="edit",
        agent_id="trading-agent",
        note_id=uuid4(),
        body="edited",
        tags=None,
        session_factory=lambda: _factory(sess),
    )
    sql = str(sess.execute.call_args.args[0])
    assert "tags" not in sql


def test_write_without_body_raises() -> None:
    with pytest.raises(ValueError, match="body"):
        manage_notes(action="write", agent_id="x")


def test_edit_without_id_raises() -> None:
    with pytest.raises(ValueError, match="note_id"):
        manage_notes(action="edit", agent_id="x", body="b")


def test_unknown_action_raises() -> None:
    with pytest.raises(ValueError, match="Unknown action"):
        manage_notes(action="delete", agent_id="x")  # type: ignore[arg-type]
