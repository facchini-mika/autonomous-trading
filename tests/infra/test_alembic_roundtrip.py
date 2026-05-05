"""End-to-end Alembic roundtrip + GRANT-matrix smoke test.

Requires a Postgres reachable at DATABASE_URL with the three app roles
already created (via `infra/sql/00_roles.sh` Docker init). Skips when no
DATABASE_URL is configured so the rest of the test suite stays runnable
without infra.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _has_database_url() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


pytestmark = pytest.mark.skipif(
    not _has_database_url(),
    reason="DATABASE_URL not set; alembic roundtrip requires a running Postgres",
)


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_upgrade_downgrade_upgrade_is_clean() -> None:
    _alembic("upgrade", "head")
    owner_url = _strip_sqlalchemy_dialect(os.environ["DATABASE_URL"])
    with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
        result = cur.fetchone()
        assert result is not None
        # 11 domain tables + 2 audit tables (Phase 6c) + alembic_version.
        assert result[0] >= 14

    _alembic("downgrade", "base")
    with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
        )
        result = cur.fetchone()
        assert result is not None
        assert result[0] == 0

    _alembic("upgrade", "head")


def test_audit_tables_have_expected_columns() -> None:
    _alembic("upgrade", "head")
    owner_url = _strip_sqlalchemy_dialect(os.environ["DATABASE_URL"])
    with psycopg.connect(owner_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='subagent_runs'"
        )
        subagent_cols = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='web_search_calls'"
        )
        web_search_cols = {r[0] for r in cur.fetchall()}
    assert {
        "id",
        "cycle_id",
        "agent_name",
        "run_id",
        "correlation_id",
        "prompt_sha",
        "started_at",
        "finished_at",
        "latency_ms",
        "cost_usd",
        "usage",
        "task_payload",
        "raw_stdout",
        "envelope",
        "error",
        "error_class",
        "created_at",
    } <= subagent_cols
    assert {
        "id",
        "run_id",
        "cycle_id",
        "agent_name",
        "query",
        "summary",
        "hits",
        "model_used",
        "elapsed_sec",
        "started_at",
        "finished_at",
        "error",
        "error_type",
        "created_at",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "web_search_count",
        "cost_usd",
    } <= web_search_cols


def test_trading_cycle_can_insert_audit_rows() -> None:
    _alembic("upgrade", "head")
    url = _role_url("trading_cycle")
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO subagent_runs "
            "(cycle_id, agent_name, run_id, prompt_sha, started_at, task_payload) "
            "VALUES ('cycle-test', 'scanner-reviewer', gen_random_uuid(), 'sha', NOW(), '{}'::jsonb)"
        )
        cur.execute("INSERT INTO web_search_calls (query, started_at) VALUES ('test', NOW())")


def _strip_sqlalchemy_dialect(url: str) -> str:
    """Convert SQLAlchemy URL (postgresql+psycopg://) to plain psycopg form."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _role_url(role: str) -> str:
    env_var = f"DATABASE_URL_{role.upper()}"
    url = os.environ.get(env_var)
    if not url:
        pytest.skip(f"{env_var} not set")
    return _strip_sqlalchemy_dialect(url)


def test_trading_cycle_can_read_lessons_but_not_write() -> None:
    _alembic("upgrade", "head")
    url = _role_url("trading_cycle")
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM lessons")
        cur.fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("INSERT INTO lessons (source_agent_id, observation, outcome) VALUES ('x','y','z')")


def test_outcome_ingestion_update_columns_enforced() -> None:
    _alembic("upgrade", "head")
    url = _role_url("outcome_ingestion")
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        # Allowed: status column.
        cur.execute("UPDATE trades SET status = 'filled' WHERE id = '00000000-0000-0000-0000-000000000000'")
        # Forbidden: size column not in the column-level GRANT.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE trades SET size = 1.0 WHERE id = '00000000-0000-0000-0000-000000000000'")


def test_lessons_summary_can_insert_but_not_update() -> None:
    _alembic("upgrade", "head")
    url = _role_url("lessons_summary")
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO lessons (source_agent_id, observation, outcome) VALUES ('a','b','c') RETURNING id")
        cur.fetchone()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE lessons SET status = 'merged'")
