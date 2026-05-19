"""Long-running Prometheus exporter daemon.

Bound to ``METRICS_HOST:METRICS_PORT`` (defaults to ``127.0.0.1:9100``)
and serves Prometheus text-format on ``/metrics``. Always localhost-only;
exposing it externally would leak live strategy and current positions.
"""

from __future__ import annotations

import signal
import sys
import threading
from typing import TYPE_CHECKING

from prometheus_client import REGISTRY, start_http_server

from shared.config.settings import Settings
from shared.logging import configure, get_logger
from shared.observability.collector import CycleMetricsCollector

if TYPE_CHECKING:
    from types import FrameType

logger = get_logger(__name__)


def main(settings: Settings | None = None) -> int:
    """Start the exporter HTTP server and block until SIGINT/SIGTERM.

    Returns 0 on clean exit (signal received), non-zero only if the bind
    itself fails. Caller (cron-managed launchd/systemd unit) interprets
    the exit code.
    """
    configure(service="metrics_exporter")
    cfg = settings if settings is not None else Settings()
    if not cfg.METRICS_ENABLED:
        logger.info("metrics_disabled_skipping_exporter")
        return 0

    REGISTRY.register(CycleMetricsCollector(orphan_max_age_minutes=cfg.ORPHAN_ATTEMPT_WARN_AFTER_MIN))
    try:
        server, _http_thread = start_http_server(addr=cfg.METRICS_HOST, port=cfg.METRICS_PORT)
    except OSError:
        logger.exception("metrics_bind_failed", host=cfg.METRICS_HOST, port=cfg.METRICS_PORT)
        return 1

    logger.info("metrics_exporter_listening", host=cfg.METRICS_HOST, port=cfg.METRICS_PORT)

    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        logger.info("metrics_exporter_signal", signum=signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    stop_event.wait()
    server.shutdown()
    logger.info("metrics_exporter_stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover — module entry, exercised via __main__.py
    sys.exit(main())
