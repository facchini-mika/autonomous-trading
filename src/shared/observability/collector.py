"""Custom Prometheus collector that builds metrics from DB rows on every scrape.

The cycle process is a 12-min cron — too short-lived to host a /metrics
endpoint itself. Instead, the cycle persists everything the metrics need
(decisions, trades, equity_snapshots, subagent_runs, system_state) and a
separate long-running daemon reads them on demand. This keeps Prometheus
on the standard pull model and the cycle hot path untouched.

Design notes
------------
* Counters are reconstructed from row counts (``decisions``, ``trades``,
  ``paper_trades``, ``subagent_runs.error``). They monotonically increase
  for as long as the tables are not truncated. Truncation would look like
  a counter reset to Prometheus, which is the same behaviour as a process
  restart of an in-memory counter — not a correctness bug, just a series-
  break the operator should be aware of.
* The histogram for ``cycle_duration_seconds`` is built with cumulative
  ``COUNT(*) FILTER (WHERE duration_seconds <= bucket)`` to satisfy the
  Prometheus rule that bucket counts are non-decreasing.
* Gauges (``equity_usd``, ``gross_exposure_usd``, ``drawdown_pct``,
  ``kill_switch_active``) read the most recent row from the relevant
  table. ``drawdown_pct`` is computed from peak-vs-current of
  ``equity_snapshots`` so no extra book-keeping is required.
* If the DB read raises, we yield only ``metrics_scrape_errors_total``
  with an incremented value and otherwise an empty surface. Prometheus
  marks the target up=0 / counter increases — both are obvious in the
  Grafana dashboard the operator will build on top.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from sqlalchemy import text

from shared.db import get_session
from shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

logger = get_logger(__name__)

# Bucket boundaries for cycle_duration_seconds. Spans well under a minute
# (cheap cycle) up past the 600s wrapper-script kill (infra/scripts/run_cycle.sh).
DURATION_BUCKETS: Final[tuple[float, ...]] = (1.0, 5.0, 30.0, 60.0, 120.0, 300.0, 600.0)

# Default agent_id for label cardinality. The MVP runs a single Lead+Trading
# agent pair; the multi-agent label space is reserved by P2.3.
DEFAULT_AGENT_ID: Final[str] = "trading-agent"


class CycleMetricsCollector:
    """Prometheus custom collector that reads cycle state from Postgres.

    Registered into ``prometheus_client.REGISTRY`` once, then ``collect()``
    is called by the WSGI handler on every Prometheus scrape.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractContextManager[Session]] | None = None,
    ) -> None:
        self._factory: Callable[[], AbstractContextManager[Session]] = (
            session_factory if session_factory is not None else _default_factory
        )
        self._scrape_errors: int = 0

    def collect(self) -> Iterator[Metric]:
        try:
            with self._factory() as session:
                yield from self._collect_all(session)
        except Exception as exc:
            self._scrape_errors += 1
            logger.warning("metrics_scrape_failed", error=str(exc), error_type=type(exc).__name__)
        # Always yield the scrape-error counter so operators can alert on it.
        scrape_err = CounterMetricFamily(
            "metrics_scrape_errors_total",
            "Number of failed Prometheus scrapes against the trading exporter.",
            value=self._scrape_errors,
        )
        yield scrape_err

    # -- per-metric assembly -------------------------------------------------

    def _collect_all(self, session: Session) -> Iterator[Metric]:
        yield self._cycle_duration(session)
        yield self._decisions_total(session)
        yield from self._trades_totals(session)
        yield self._errors_total(session)
        yield self._equity_usd(session)
        yield self._gross_exposure_usd(session)
        yield self._drawdown_pct(session)
        yield self._kill_switch_active(session)
        yield from self._agent_performance_gauges(session)

    def _agent_performance_gauges(self, session: Session) -> Iterator[Metric]:
        """Emit four per-agent gauges from the latest ``agent_performance`` row.

        Spec: ``trading_feedback.md §6`` and ``data_infrastructure.md §1``.
        Tier-1 writes one row per agent per cycle; we read the latest row per
        agent via ``DISTINCT ON``. NULL values for ``hit_rate_30d`` /
        ``sharpe_30d`` are omitted (Prometheus does not have a NaN-safe gauge
        idiom; absence is semantically correct).
        """
        rows = session.execute(
            text(
                """
                SELECT DISTINCT ON (agent_id)
                    agent_id, hit_rate_30d, sharpe_30d, pnl_30d, n_samples
                FROM agent_performance
                ORDER BY agent_id, time DESC
                """,
            ),
        ).all()

        hit_rate = GaugeMetricFamily(
            "agent_hit_rate_30d",
            "Fraction of resolved predictions where the agent's side bet matched outcome (rolling 30d).",
            labels=["agent_id"],
        )
        sharpe = GaugeMetricFamily(
            "agent_sharpe_30d",
            "Window-relative sharpe ratio of agent's per-trade realized PnL (rolling 30d, no annualisation).",
            labels=["agent_id"],
        )
        pnl = GaugeMetricFamily(
            "agent_pnl_30d_usd",
            "Sum of realized PnL on the agent's resolved predictions (rolling 30d).",
            labels=["agent_id"],
        )
        samples = GaugeMetricFamily(
            "agent_n_samples_30d",
            "Number of resolved predictions feeding the rolling 30d window.",
            labels=["agent_id"],
        )
        for r in rows:
            label = [str(r.agent_id)]
            if r.hit_rate_30d is not None:
                hit_rate.add_metric(label, float(r.hit_rate_30d))
            if r.sharpe_30d is not None:
                sharpe.add_metric(label, float(r.sharpe_30d))
            pnl.add_metric(label, float(r.pnl_30d))
            samples.add_metric(label, float(r.n_samples))
        yield hit_rate
        yield sharpe
        yield pnl
        yield samples

    def _cycle_duration(self, session: Session) -> Metric:
        # Cumulative bucket counts via FILTER. Postgres returns them all in one
        # round-trip; SQLAlchemy gives us a row with named columns.
        select_clauses = ", ".join(
            f"COUNT(*) FILTER (WHERE duration_seconds <= {b}) AS le_{i}" for i, b in enumerate(DURATION_BUCKETS)
        )
        row = session.execute(
            text(
                f"SELECT {select_clauses}, COUNT(*) AS total, "
                "COALESCE(SUM(duration_seconds), 0) AS sum_seconds "
                "FROM equity_snapshots",
            ),
        ).first()
        bucket_pairs: list[tuple[str, float]]
        if row is None:
            bucket_pairs = [(str(b), 0.0) for b in DURATION_BUCKETS]
            bucket_pairs.append(("+Inf", 0.0))
            sum_value = 0.0
        else:
            bucket_pairs = [(str(b), float(getattr(row, f"le_{i}"))) for i, b in enumerate(DURATION_BUCKETS)]
            bucket_pairs.append(("+Inf", float(row.total)))
            sum_value = float(row.sum_seconds)
        family = HistogramMetricFamily(
            "cycle_duration_seconds",
            "Wall-clock duration of one Lead-orchestrated trading cycle.",
            labels=["agent_id"],
        )
        family.add_metric([DEFAULT_AGENT_ID], buckets=bucket_pairs, sum_value=sum_value)
        return family

    def _decisions_total(self, session: Session) -> Metric:
        rows = session.execute(
            text("SELECT action, COUNT(*) AS n FROM decisions GROUP BY action"),
        ).all()
        family = CounterMetricFamily(
            "decisions_total",
            "Number of risk-execution decisions, partitioned by action.",
            labels=["agent_id", "action"],
        )
        seen: set[str] = set()
        for r in rows:
            family.add_metric([DEFAULT_AGENT_ID, str(r.action)], float(r.n))
            seen.add(str(r.action))
        # Always emit the three actions even if zero — keeps Grafana queries
        # from going NaN on a fresh DB.
        for action in ("trade", "skip", "hold"):
            if action not in seen:
                family.add_metric([DEFAULT_AGENT_ID, action], 0.0)
        return family

    def _trades_totals(self, session: Session) -> Iterator[Metric]:
        rows = session.execute(
            text(
                """
                SELECT 'real_capital' AS mode, side, status, COUNT(*) AS n
                FROM trades GROUP BY side, status
                UNION ALL
                SELECT 'paper' AS mode, side, status, COUNT(*) AS n
                FROM paper_trades GROUP BY side, status
                """,
            ),
        ).all()
        family = CounterMetricFamily(
            "trades_total",
            "Number of trade rows recorded, partitioned by mode/side/status.",
            labels=["agent_id", "mode", "side", "status"],
        )
        for r in rows:
            family.add_metric(
                [DEFAULT_AGENT_ID, str(r.mode), str(r.side), str(r.status)],
                float(r.n),
            )
        yield family

    def _errors_total(self, session: Session) -> Metric:
        rows = session.execute(
            text(
                """
                SELECT agent_name AS source,
                       COALESCE(error_class, 'unknown') AS error_type,
                       COUNT(*) AS n
                FROM subagent_runs
                WHERE error IS NOT NULL
                GROUP BY agent_name, error_class
                """,
            ),
        ).all()
        family = CounterMetricFamily(
            "errors_total",
            "Number of error events recorded against any subagent run.",
            labels=["agent_id", "source", "error_type"],
        )
        for r in rows:
            family.add_metric(
                [DEFAULT_AGENT_ID, str(r.source), str(r.error_type)],
                float(r.n),
            )
        return family

    def _equity_usd(self, session: Session) -> Metric:
        row = session.execute(
            text("SELECT equity_usd FROM equity_snapshots ORDER BY time DESC LIMIT 1"),
        ).first()
        value = float(row.equity_usd) if row is not None else 0.0
        family = GaugeMetricFamily(
            "equity_usd",
            "Current account equity in USD (latest equity_snapshots row).",
            labels=["agent_id"],
        )
        family.add_metric([DEFAULT_AGENT_ID], value)
        return family

    def _gross_exposure_usd(self, session: Session) -> Metric:
        row = session.execute(
            text("SELECT gross_exposure_usd FROM equity_snapshots ORDER BY time DESC LIMIT 1"),
        ).first()
        value = float(row.gross_exposure_usd) if row is not None else 0.0
        family = GaugeMetricFamily(
            "gross_exposure_usd",
            "Sum of |position notional| in USD (latest equity_snapshots row).",
            labels=["agent_id"],
        )
        family.add_metric([DEFAULT_AGENT_ID], value)
        return family

    def _drawdown_pct(self, session: Session) -> Metric:
        row = session.execute(
            text(
                "SELECT equity_usd, peak_equity_usd FROM equity_snapshots ORDER BY time DESC LIMIT 1",
            ),
        ).first()
        if row is None or float(row.peak_equity_usd) <= 0:
            value = 0.0
        else:
            peak = float(row.peak_equity_usd)
            current = float(row.equity_usd)
            value = max((peak - current) / peak, 0.0)
        family = GaugeMetricFamily(
            "drawdown_pct",
            "Current drawdown as fraction of peak equity (0..1).",
            labels=["agent_id"],
        )
        family.add_metric([DEFAULT_AGENT_ID], value)
        return family

    def _kill_switch_active(self, session: Session) -> Metric:
        row = session.execute(
            text("SELECT value FROM system_state WHERE key = 'kill_switch'"),
        ).first()
        active = 0.0
        if row is not None:
            value = row.value
            if isinstance(value, bool):
                active = 1.0 if value else 0.0
            elif isinstance(value, dict):
                flag = value.get("active") or value.get("value")
                active = 1.0 if bool(flag) else 0.0
            else:
                active = 1.0 if bool(value) else 0.0
        family = GaugeMetricFamily(
            "kill_switch_active",
            "1 if the kill switch is currently armed, else 0.",
            labels=["agent_id"],
        )
        family.add_metric([DEFAULT_AGENT_ID], active)
        return family


def _default_factory() -> AbstractContextManager[Session]:
    return get_session("trading_cycle")
