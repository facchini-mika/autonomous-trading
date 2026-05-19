"""DB-gated tests for `DbIdempotencyStore` against a real Postgres.

The store has three observable behaviours that only emerge against
real SQL: ``ON CONFLICT DO NOTHING`` semantics for ``reserve``,
``finished_at`` filtering in ``get``, and the partial-update shape of
``put``. Mocking SQLAlchemy would test against the mock, not Postgres,
so this skips when ``DATABASE_URL_TRADING_CYCLE`` is missing — exactly
the same gate used by ``tests/infra/test_openai_cost_report_db.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from shared.adapters.db_idempotency_store import DbIdempotencyStore
from shared.models import OrderResult

SessionFactory = Callable[[], AbstractContextManager[Session]]


def _has_role_url() -> bool:
    return bool(os.environ.get("DATABASE_URL_TRADING_CYCLE"))


pytestmark = pytest.mark.skipif(
    not _has_role_url(),
    reason="DATABASE_URL_TRADING_CYCLE not set; DbIdempotencyStore needs a real Postgres",
)


@pytest.fixture
def session_factory() -> SessionFactory:
    url = os.environ["DATABASE_URL_TRADING_CYCLE"]
    engine = create_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def make_session() -> Iterator[Session]:
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return make_session


@pytest.fixture
def store(session_factory: SessionFactory) -> DbIdempotencyStore:
    return DbIdempotencyStore(session_factory)


@pytest.fixture
def fresh_key() -> str:
    return f"test:{uuid.uuid4()}"


def test_reserve_first_call_returns_true(store: DbIdempotencyStore, fresh_key: str) -> None:
    assert store.reserve(fresh_key, cycle_id="cycle-test", decision_id=str(uuid.uuid4())) is True


def test_reserve_second_call_returns_false(store: DbIdempotencyStore, fresh_key: str) -> None:
    decision_id = str(uuid.uuid4())
    assert store.reserve(fresh_key, cycle_id="cycle-1", decision_id=decision_id) is True
    assert store.reserve(fresh_key, cycle_id="cycle-2", decision_id=decision_id) is False


def test_get_returns_none_for_unreserved_key(store: DbIdempotencyStore, fresh_key: str) -> None:
    assert store.get(fresh_key) is None


def test_get_returns_none_for_pending_row(store: DbIdempotencyStore, fresh_key: str) -> None:
    """Pending (not-yet-finalised) rows must not surface via `get`.

    Treating them as cache hits would mask a crashed mid-submit attempt;
    `reserve` is the channel that signals the conflict instead.
    """
    store.reserve(fresh_key, cycle_id="cycle-pending", decision_id=str(uuid.uuid4()))
    assert store.get(fresh_key) is None


def test_put_finalises_and_get_returns_terminal(
    store: DbIdempotencyStore,
    fresh_key: str,
) -> None:
    store.reserve(fresh_key, cycle_id="cycle-final", decision_id=str(uuid.uuid4()))
    result = OrderResult(
        status="filled",
        broker_order_id="0xdeadbeef",
        fill_price=0.55,
        filled_size=2.5,
        fees=0.01,
    )
    store.put(fresh_key, result)
    got = store.get(fresh_key)
    assert got is not None
    assert got.status == "filled"
    assert got.broker_order_id == "0xdeadbeef"
    assert got.fill_price == pytest.approx(0.55)
    assert got.filled_size == pytest.approx(2.5)


def test_put_records_error_metadata(
    store: DbIdempotencyStore,
    fresh_key: str,
    session_factory: SessionFactory,
) -> None:
    store.reserve(fresh_key, cycle_id="cycle-err", decision_id=str(uuid.uuid4()))
    rejected = OrderResult(status="rejected", broker_order_id=None, fill_price=None, filled_size=0.0)
    store.put(fresh_key, rejected, error_class="PolymarketPermanentError", error_message="400 invalid")
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT status, error_class, error_message, finished_at FROM order_attempts WHERE idempotency_key = :k",
            ),
            {"k": fresh_key},
        ).first()
    assert row is not None
    assert row.status == "rejected"
    assert row.error_class == "PolymarketPermanentError"
    assert row.error_message == "400 invalid"
    assert row.finished_at is not None


def test_reserve_after_put_still_returns_false(
    store: DbIdempotencyStore,
    fresh_key: str,
) -> None:
    """A finalised key cannot be reserved again — `get` is the right channel."""
    store.reserve(fresh_key, cycle_id="cycle-done", decision_id=str(uuid.uuid4()))
    store.put(fresh_key, OrderResult(status="filled", broker_order_id="x", filled_size=1.0))
    assert store.reserve(fresh_key, cycle_id="cycle-replay", decision_id=str(uuid.uuid4())) is False
