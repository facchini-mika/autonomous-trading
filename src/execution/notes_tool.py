"""LRU-bounded `manage_notes` tool for the trading-agent.

Per `specs/trading.md §59`, every agent has at most 50 notes. Reads update
`last_accessed`; writes either INSERT a new note or UPDATE an existing one.
When the cap is hit, the oldest-by-`last_accessed` row is evicted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import text

from shared.db import get_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_MAX_NOTES: Final = 50

NoteAction = Literal["read", "write", "edit"]


def manage_notes(
    *,
    action: NoteAction,
    agent_id: str,
    body: str | None = None,
    note_id: UUID | None = None,
    tags: list[str] | None = None,
    max_notes: int = DEFAULT_MAX_NOTES,
    session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> list[dict[str, object]]:
    """Read, write, or edit notes for `agent_id`.

    - `read` → returns up to `max_notes` rows (most-recent-first); updates
      `last_accessed` for the returned rows.
    - `write` → INSERTs a new row; evicts the oldest if at the cap.
    - `edit` → UPDATEs `body`/`tags` on `note_id`; updates `last_accessed`.
    """
    factory = session_factory or _default_factory
    now = (clock or _utcnow)()

    if action == "read":
        return _read_notes(agent_id=agent_id, factory=factory, now=now)
    if action == "write":
        if body is None:
            msg = "write action requires `body`"
            raise ValueError(msg)
        return _write_note(
            agent_id=agent_id,
            body=body,
            tags=tags or [],
            max_notes=max_notes,
            factory=factory,
            now=now,
        )
    if action == "edit":
        if note_id is None or body is None:
            msg = "edit action requires both `note_id` and `body`"
            raise ValueError(msg)
        return _edit_note(
            agent_id=agent_id,
            note_id=note_id,
            body=body,
            tags=tags,
            factory=factory,
            now=now,
        )
    msg = f"Unknown action: {action}"  # type: ignore[unreachable]
    raise ValueError(msg)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("trading_cycle")


def _read_notes(
    *,
    agent_id: str,
    factory: Callable[[], AbstractContextManager[Session]],
    now: datetime,
) -> list[dict[str, object]]:
    with factory() as session:
        rows = session.execute(
            text(
                """
                SELECT id, body, tags, last_accessed, created_at
                FROM notes
                WHERE agent_id = :agent_id
                ORDER BY last_accessed DESC
                LIMIT :limit
                """,
            ),
            {"agent_id": agent_id, "limit": DEFAULT_MAX_NOTES},
        ).all()
        ids = [str(r.id) for r in rows]
        if ids:
            session.execute(
                text("UPDATE notes SET last_accessed = :now WHERE id = ANY(:ids)"),
                {"now": now, "ids": ids},
            )
        return [_row_to_dict(r) for r in rows]


def _write_note(
    *,
    agent_id: str,
    body: str,
    tags: list[str],
    max_notes: int,
    factory: Callable[[], AbstractContextManager[Session]],
    now: datetime,
) -> list[dict[str, object]]:
    new_id = uuid4()
    with factory() as session:
        count_row = session.execute(
            text("SELECT COUNT(*) FROM notes WHERE agent_id = :agent_id"),
            {"agent_id": agent_id},
        ).first()
        count = int(count_row[0]) if count_row else 0
        if count >= max_notes:
            session.execute(
                text(
                    """
                    DELETE FROM notes
                    WHERE id = (
                        SELECT id FROM notes WHERE agent_id = :agent_id
                        ORDER BY last_accessed ASC
                        LIMIT 1
                    )
                    """,
                ),
                {"agent_id": agent_id},
            )
        session.execute(
            text(
                """
                INSERT INTO notes (id, agent_id, body, tags, last_accessed, created_at)
                VALUES (:id, :agent_id, :body, :tags, :now, :now)
                """,
            ),
            {
                "id": str(new_id),
                "agent_id": agent_id,
                "body": body,
                "tags": tags,
                "now": now,
            },
        )
    return [{"id": str(new_id), "agent_id": agent_id, "body": body, "tags": tags}]


def _edit_note(
    *,
    agent_id: str,
    note_id: UUID,
    body: str,
    tags: list[str] | None,
    factory: Callable[[], AbstractContextManager[Session]],
    now: datetime,
) -> list[dict[str, object]]:
    with factory() as session:
        if tags is None:
            session.execute(
                text(
                    """
                    UPDATE notes
                    SET body = :body, last_accessed = :now
                    WHERE id = :id AND agent_id = :agent_id
                    """,
                ),
                {"body": body, "now": now, "id": str(note_id), "agent_id": agent_id},
            )
        else:
            session.execute(
                text(
                    """
                    UPDATE notes
                    SET body = :body, tags = :tags, last_accessed = :now
                    WHERE id = :id AND agent_id = :agent_id
                    """,
                ),
                {
                    "body": body,
                    "tags": tags,
                    "now": now,
                    "id": str(note_id),
                    "agent_id": agent_id,
                },
            )
    return [{"id": str(note_id), "agent_id": agent_id, "body": body}]


def _row_to_dict(row: object) -> dict[str, object]:
    return {
        "id": str(getattr(row, "id", "")),
        "body": getattr(row, "body", ""),
        "tags": list(getattr(row, "tags", []) or []),
        "last_accessed": getattr(row, "last_accessed", None),
        "created_at": getattr(row, "created_at", None),
    }
