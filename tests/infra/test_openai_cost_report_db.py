"""DB-gated smoke test for the cost-report SQL.

The unit tests in ``tests/execution/test_openai_cost_report.py`` exercise
``render_table`` / ``render_json`` against fake ``CycleRow`` objects, but
cannot catch SQL bugs that only manifest at psycopg parse time
(e.g. ``AmbiguousParameter`` when a nullable parameter has no type hint).
This test runs the real query against a live Postgres so the path goes
through psycopg's parameter-type inference.

Skips when ``DATABASE_URL_TRADING_CYCLE`` is not configured.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from execution.openai_cost_report import fetch_rows


def _has_role_url() -> bool:
    return bool(os.environ.get("DATABASE_URL_TRADING_CYCLE"))


pytestmark = pytest.mark.skipif(
    not _has_role_url(),
    reason="DATABASE_URL_TRADING_CYCLE not set; cost-report SQL needs a real Postgres",
)


@pytest.fixture
def session() -> Iterator[Session]:
    url = os.environ["DATABASE_URL_TRADING_CYCLE"]
    engine = create_engine(url)
    factory = sessionmaker(bind=engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()


def test_fetch_rows_with_no_filter(session: Session) -> None:
    """All-NULL params must not trip psycopg's parameter-type inference."""
    rows = fetch_rows(session, cycle_id=None, since=None)
    assert isinstance(rows, list)


def test_fetch_rows_with_cycle_filter(session: Session) -> None:
    rows = fetch_rows(session, cycle_id="does-not-exist", since=None)
    assert rows == []


def test_fetch_rows_with_since_filter(session: Session) -> None:
    rows = fetch_rows(
        session,
        cycle_id=None,
        since=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert rows == []


def test_fetch_rows_with_both_filters(session: Session) -> None:
    rows = fetch_rows(
        session,
        cycle_id="does-not-exist",
        since=datetime(2030, 1, 1, tzinfo=UTC),
    )
    assert rows == []
