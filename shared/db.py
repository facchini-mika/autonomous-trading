"""Database session helper — picks per-role connection URL from env.

Every cron-driven process opens sessions only for its assigned role per
`engineering.md §1` and `orchestration.md §1`. Mixing roles in one process
defeats the column-level GRANT enforcement.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DbRole = Literal["trading_cycle", "outcome_ingestion", "lessons_summary"]

_engine_cache: dict[DbRole, Engine] = {}


def _engine(role: DbRole) -> Engine:
    if role not in _engine_cache:
        env_var = f"DATABASE_URL_{role.upper()}"
        url = os.environ.get(env_var)
        if not url:
            msg = f"Missing env var {env_var} required for role {role}"
            raise RuntimeError(msg)
        _engine_cache[role] = create_engine(
            url,
            pool_size=5,
            pool_recycle=300,
            pool_pre_ping=True,
        )
    return _engine_cache[role]


@contextmanager
def get_session(role: DbRole) -> Iterator[Session]:
    """Yield a SQLAlchemy session bound to the named role's connection.

    Always use as a context manager so the session is closed and the engine
    pool is returned even on error.
    """
    factory = sessionmaker(bind=_engine(role), expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
