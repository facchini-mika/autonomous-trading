"""E2E fixtures: skip when no DB env, truncate all 11 tables before each test."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_TABLES = (
    "markets",
    "market_snapshots",
    "predictions",
    "decisions",
    "trades",
    "paper_trades",
    "positions",
    "notes",
    "cycle_plan",
    "lessons",
    "system_state",
    "equity_snapshots",
)


def _has_database_url() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


pytestmark = pytest.mark.skipif(
    not _has_database_url(),
    reason="E2E paper-cycle requires Postgres (DATABASE_URL + per-role URLs)",
)


def _strip_dialect(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session", autouse=True)
def _migrations_applied() -> Iterator[None]:
    """Apply migrations once for the E2E session."""
    if not _has_database_url():
        yield
        return
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    yield


@pytest.fixture
def clean_db() -> Iterator[None]:
    """Truncate all 11 tables before each test using the owner connection."""
    if not _has_database_url():
        yield
        return
    owner_url = _strip_dialect(os.environ["DATABASE_URL"])
    with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} CASCADE")
        conn.commit()
    yield


@pytest.fixture
def owner_conn() -> Iterator[psycopg.Connection]:
    """psycopg connection bound to the owner role for read-side assertions."""
    if not _has_database_url():
        pytest.skip("DATABASE_URL not set")
    url = _strip_dialect(os.environ["DATABASE_URL"])
    with psycopg.connect(url) as conn:
        yield conn
