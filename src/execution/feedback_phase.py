"""Inline feedback phase — runs before the trading-agent each cycle.

Closes the trade -> outcome -> lesson -> decision loop in a single
``trading_cycle`` run. The Lead invokes ``run_feedback_phase`` immediately
after binding ``cycle_id`` and before scanner / trading-agent dispatch so
freshly-resolved outcomes and newly-derived lessons land in the
``TradingAgentTask`` payload that follows.

Each sub-step is fail-isolated: an exception from outcome ingestion never
prevents lessons from running, and a failure in either never prevents the
trading-cycle itself from proceeding. The standalone ``outcome_ingestion``
and ``lessons_summary`` crons remain as an idempotent safety net.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from execution import outcome_ingestion
from execution.evaluation_bootstrap import HIGH_WATER_MARK_KEY as EVAL_HWM_KEY
from execution.gamma_client import GammaClient
from execution.run_evaluation import run_inline as _run_tier1_eval_inline
from research.skills import lessons_summary
from shared.db import get_session
from shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

    from shared.config.settings import Settings

logger = get_logger(__name__)


@dataclass
class FeedbackPhaseResult:
    """Outcome of one inline feedback pass; logged + asserted on in tests."""

    cycle_id: str
    outcome_counts: dict[str, int] = field(default_factory=dict)
    lessons_inserted: int = 0
    tier1_eval_ran: bool = False
    tier1_eval_rows: int = 0
    outcome_error: str | None = None
    lessons_error: str | None = None
    tier1_eval_error: str | None = None
    skipped: bool = False

    def summary(self) -> dict[str, Any]:
        """Flat log-friendly summary."""
        return {
            "outcome_counts": self.outcome_counts,
            "lessons_inserted": self.lessons_inserted,
            "tier1_eval_ran": self.tier1_eval_ran,
            "tier1_eval_rows": self.tier1_eval_rows,
            "outcome_error": self.outcome_error,
            "lessons_error": self.lessons_error,
            "tier1_eval_error": self.tier1_eval_error,
            "skipped": self.skipped,
        }


def run_feedback_phase(
    *,
    settings: Settings,
    cycle_id: str,
    now: datetime,
    gamma_factory: Callable[[], GammaClient] | None = None,
    outcome_session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    lessons_session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    eval_session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    tier1_runner: Callable[[], int] | None = None,
) -> FeedbackPhaseResult:
    """Run outcome ingestion + lessons summary + optional Tier-1 eval inline.

    Order matters: outcome ingestion must precede lessons (the heuristic only
    sees predictions whose ``outcome`` column is set), and both must precede
    the trading-agent so the fresh lessons reach ``_collect_trading_context``.
    """
    result = FeedbackPhaseResult(cycle_id=cycle_id)
    if not settings.INLINE_FEEDBACK_ENABLED:
        result.skipped = True
        logger.info("feedback_phase_skipped", cycle_id=cycle_id, reason="master_flag_off")
        return result

    def clock() -> datetime:
        return now

    timeout = settings.INLINE_FEEDBACK_TIMEOUT_SEC

    if settings.INLINE_OUTCOME_INGESTION_ENABLED:
        try:
            counts = _run_with_timeout(
                lambda: _run_outcome_ingestion(
                    settings=settings,
                    clock=clock,
                    gamma_factory=gamma_factory,
                    session_factory=outcome_session_factory,
                ),
                timeout_sec=timeout,
            )
            result.outcome_counts = counts
            logger.info("inline_outcome_ingestion_done", cycle_id=cycle_id, counts=counts)
        except Exception as exc:
            result.outcome_error = repr(exc)
            logger.warning("inline_outcome_ingestion_failed", cycle_id=cycle_id, error=repr(exc))

    if settings.INLINE_LESSONS_SUMMARY_ENABLED:
        try:
            inserted = _run_with_timeout(
                lambda: lessons_summary.run_once(
                    settings=settings,
                    session_factory=lessons_session_factory,
                    clock=clock,
                ),
                timeout_sec=timeout,
            )
            result.lessons_inserted = inserted
            logger.info("inline_lessons_summary_done", cycle_id=cycle_id, inserted=inserted)
        except Exception as exc:
            result.lessons_error = repr(exc)
            logger.warning("inline_lessons_summary_failed", cycle_id=cycle_id, error=repr(exc))

    if settings.INLINE_TIER1_EVAL_ENABLED and _should_run_tier1_eval(
        settings=settings,
        now=now,
        session_factory=eval_session_factory,
    ):
        try:
            rows = _run_with_timeout(
                tier1_runner if tier1_runner is not None else _default_tier1_runner,
                timeout_sec=max(timeout, settings.EVALUATION_TIMEOUT_SEC),
            )
            result.tier1_eval_ran = True
            result.tier1_eval_rows = rows
            logger.info("inline_tier1_eval_done", cycle_id=cycle_id, rows=rows)
        except Exception as exc:
            result.tier1_eval_error = repr(exc)
            logger.warning("inline_tier1_eval_failed", cycle_id=cycle_id, error=repr(exc))

    logger.info("feedback_phase_done", cycle_id=cycle_id, **result.summary())
    return result


def _run_outcome_ingestion(
    *,
    settings: Settings,
    clock: Callable[[], datetime],
    gamma_factory: Callable[[], GammaClient] | None,
    session_factory: Callable[[], AbstractContextManager[Session]] | None,
) -> dict[str, int]:
    factory = gamma_factory or _default_gamma_factory
    gamma = factory()
    try:
        return outcome_ingestion.run_once(
            gamma=gamma,
            session_factory=session_factory,
            lookback_days=settings.INLINE_OUTCOME_LOOKBACK_DAYS,
            clock=clock,
        )
    finally:
        # Only owned clients should be closed; if the caller injected one via
        # gamma_factory they're responsible for its lifecycle.
        if gamma_factory is None:
            gamma.close()


def _default_gamma_factory() -> GammaClient:
    return GammaClient()


def _should_run_tier1_eval(
    *,
    settings: Settings,
    now: datetime,
    session_factory: Callable[[], AbstractContextManager[Session]] | None,
) -> bool:
    """True iff ``last_evaluation_at`` is missing or older than the interval."""
    factory = session_factory or _default_eval_factory
    interval = timedelta(minutes=settings.INLINE_TIER1_EVAL_INTERVAL_MIN)
    last = _read_eval_hwm(factory)
    if last is None:
        return True
    return (now - last) >= interval


def _default_eval_factory() -> AbstractContextManager[Session]:
    # The evaluation lead uses the ``lessons_summary`` role for HWM reads
    # (see evaluation_bootstrap._default_factory); we mirror that here so
    # the read goes through the same authority boundary.
    return get_session("lessons_summary")


def _read_eval_hwm(
    factory: Callable[[], AbstractContextManager[Session]],
) -> datetime | None:
    with factory() as session:
        row = session.execute(
            text("SELECT value FROM system_state WHERE key = :key"),
            {"key": EVAL_HWM_KEY},
        ).first()
    if row is None:
        return None
    value = row.value
    iso: str | None = None
    if isinstance(value, str):
        iso = value
    elif isinstance(value, dict):
        candidate = value.get("at") or value.get("value") or value.get("ts")
        if isinstance(candidate, str):
            iso = candidate
    if iso is None:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _default_tier1_runner() -> int:
    artifacts = _run_tier1_eval_inline()
    return len(artifacts.rows)


def _run_with_timeout(fn: Callable[[], Any], *, timeout_sec: int) -> Any:
    """Run ``fn`` on a worker thread, raising on timeout.

    ThreadPoolExecutor is used because ``outcome_ingestion`` is synchronous
    httpx + SQLAlchemy and SIGALRM is unsafe on worker threads / on macOS
    subprocess hosts. On timeout the thread keeps running until httpx
    returns; the leaked thread dies with the next-cycle PID rollover.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeoutError as exc:
            msg = f"feedback step exceeded {timeout_sec}s"
            raise TimeoutError(msg) from exc
