"""Structured-logging configuration for the autonomous-trading services.

JSON-on-stdout in MVP per ``data_infrastructure.md §3``. Required per-line
fields: ``timestamp, service, level, cycle_id?, correlation_id?, market_id?,
agent_id?, decision_id?, event, ...payload``. Reasoning traces stay in
``predictions.inference_log`` JSONB; logs carry only the breadcrumbs.

Usage::

    from shared.logging import configure, bind, get_logger

    configure(service="trading_cycle")
    bind(correlation_id="abc123")
    log = get_logger(__name__)
    log.info("cycle_starting", cycle_id="cycle-1")
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any, Final

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
    unbind_contextvars,
)
from structlog.processors import JSONRenderer, TimeStamper, format_exc_info
from structlog.stdlib import add_log_level

if TYPE_CHECKING:
    from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

_SERVICE_KEY: Final = "service"


def configure(*, service: str, level: int = logging.INFO) -> None:
    """Configure structlog for one process. Idempotent across calls."""
    structlog.configure(
        processors=[
            merge_contextvars,
            add_log_level,
            TimeStamper(fmt="iso", utc=True, key="timestamp"),
            format_exc_info,
            _make_service_processor(service),
            JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s", force=True)


def _make_service_processor(
    service: str,
) -> Any:
    def add_service(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
        event[_SERVICE_KEY] = service
        return event

    return add_service


def bind(**kwargs: Any) -> None:
    """Bind context fields visible in every subsequent log entry on this task."""
    bind_contextvars(**kwargs)


def unbind(*keys: str) -> None:
    unbind_contextvars(*keys)


def clear() -> None:
    clear_contextvars()


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger
