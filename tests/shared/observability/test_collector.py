"""Unit tests for the Prometheus CycleMetricsCollector.

These tests do not touch a real database. A ``FakeSession`` is injected so
each metric is exercised against canned rows. The HTTP-roundtrip tests
live in ``test_exporter.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)

from shared.observability.collector import (
    DEFAULT_AGENT_ID,
    DURATION_BUCKETS,
    CycleMetricsCollector,
)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Returns canned rows based on substring match against the SQL text.

    Order of insertion matters — the first key found in the SQL wins. Use
    long, distinctive substrings to avoid false matches. Unmatched queries
    return an empty result (the common case in collector tests where most
    tables are intentionally empty).
    """

    def __init__(self, responses: list[tuple[str, list[Any]]]) -> None:
        self._responses = responses

    def execute(self, query: object, params: object | None = None) -> _FakeResult:
        del params  # ignored; substring routing is enough for the fake.
        sql = str(query)
        for key, rows in self._responses:
            if key in sql:
                return _FakeResult(rows)
        return _FakeResult([])


@contextmanager
def _fake_ctx(session: _FakeSession) -> Iterator[_FakeSession]:
    yield session


def _build_collector(responses: list[tuple[str, list[Any]]]) -> CycleMetricsCollector:
    session = _FakeSession(responses)
    factory = cast("Any", lambda: _fake_ctx(session))
    return CycleMetricsCollector(session_factory=factory)


def _names(metrics: list[Metric]) -> list[str]:
    return [m.name for m in metrics]


def test_empty_db_yields_all_metrics_with_zero_or_default_values() -> None:
    collector = _build_collector(
        [
            ("FROM equity_snapshots", []),
            ("FROM decisions", []),
            ("FROM trades", []),
            ("FROM subagent_runs", []),
            ("FROM system_state", []),
        ],
    )
    metrics = list(collector.collect())
    names = _names(metrics)
    # All eight spec metrics + the scrape-error counter must be present.
    expected = {
        "cycle_duration_seconds",
        "decisions",
        "trades",
        "errors",
        "equity_usd",
        "gross_exposure_usd",
        "drawdown_pct",
        "kill_switch_active",
        "metrics_scrape_errors",
    }
    assert expected <= set(names)


def test_decisions_zero_pads_three_actions() -> None:
    collector = _build_collector(
        [
            ("FROM equity_snapshots", []),
            (
                "FROM decisions",
                [SimpleNamespace(action="trade", n=4)],
            ),
            ("FROM trades", []),
            ("FROM subagent_runs", []),
            ("FROM system_state", []),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}
    decisions = metrics["decisions"]
    assert isinstance(decisions, CounterMetricFamily)
    by_action = {s.labels["action"]: s.value for s in decisions.samples if s.name.endswith("_total")}
    assert by_action == {"trade": 4.0, "skip": 0.0, "hold": 0.0}


def test_trades_total_includes_paper_and_real_modes() -> None:
    rows = [
        SimpleNamespace(mode="real_capital", side="yes", status="filled", n=2),
        SimpleNamespace(mode="paper", side="no", status="filled", n=5),
    ]
    collector = _build_collector(
        [
            ("FROM equity_snapshots", []),
            ("FROM decisions", []),
            ("FROM trades", rows),
            ("FROM subagent_runs", []),
            ("FROM system_state", []),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}
    trades = metrics["trades"]
    assert isinstance(trades, CounterMetricFamily)
    samples = {(s.labels["mode"], s.labels["side"], s.labels["status"]): s.value for s in trades.samples}
    assert samples[("real_capital", "yes", "filled")] == 2.0
    assert samples[("paper", "no", "filled")] == 5.0


def test_errors_total_groups_by_source_and_type() -> None:
    rows = [
        SimpleNamespace(source="trading-agent", error_type="SubagentBudgetError", n=1),
        SimpleNamespace(source="scanner-reviewer", error_type="unknown", n=2),
    ]
    collector = _build_collector(
        [
            ("FROM equity_snapshots", []),
            ("FROM decisions", []),
            ("FROM trades", []),
            ("FROM subagent_runs", rows),
            ("FROM system_state", []),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}
    errors = metrics["errors"]
    assert isinstance(errors, CounterMetricFamily)
    by_source = {(s.labels["source"], s.labels["error_type"]): s.value for s in errors.samples}
    assert by_source[("trading-agent", "SubagentBudgetError")] == 1.0
    assert by_source[("scanner-reviewer", "unknown")] == 2.0


def test_equity_and_drawdown_use_latest_snapshot() -> None:
    snapshot_row = SimpleNamespace(
        equity_usd=900.0,
        peak_equity_usd=1000.0,
        gross_exposure_usd=250.0,
    )
    # The duration query is matched first; we keep its rows empty so it
    # contributes zero buckets, then the equity/exposure/drawdown queries
    # all match the same "FROM equity_snapshots" key. The fake session
    # returns the same row for each — close enough for unit-level shape.
    collector = _build_collector(
        [
            (
                "ORDER BY time DESC LIMIT 1",  # equity, gross_exposure, drawdown
                [snapshot_row],
            ),
            ("FROM equity_snapshots", []),  # cycle_duration histogram (empty)
            ("FROM decisions", []),
            ("FROM trades", []),
            ("FROM subagent_runs", []),
            ("FROM system_state", []),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}

    equity = metrics["equity_usd"]
    assert isinstance(equity, GaugeMetricFamily)
    assert equity.samples[0].value == 900.0
    assert equity.samples[0].labels["agent_id"] == DEFAULT_AGENT_ID

    exposure = metrics["gross_exposure_usd"]
    assert isinstance(exposure, GaugeMetricFamily)
    assert exposure.samples[0].value == 250.0

    drawdown = metrics["drawdown_pct"]
    assert isinstance(drawdown, GaugeMetricFamily)
    # (1000 - 900) / 1000 = 0.1
    assert drawdown.samples[0].value == 0.1


def test_drawdown_zero_when_no_snapshot() -> None:
    collector = _build_collector(
        [
            ("FROM equity_snapshots", []),
            ("FROM decisions", []),
            ("FROM trades", []),
            ("FROM subagent_runs", []),
            ("FROM system_state", []),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}
    drawdown = metrics["drawdown_pct"]
    assert isinstance(drawdown, GaugeMetricFamily)
    assert drawdown.samples[0].value == 0.0


def test_kill_switch_active_reads_jsonb_active_key() -> None:
    collector = _build_collector(
        [
            ("FROM equity_snapshots", []),
            ("FROM decisions", []),
            ("FROM trades", []),
            ("FROM subagent_runs", []),
            (
                "FROM system_state",
                [SimpleNamespace(value={"active": True, "reason": "manual test"})],
            ),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}
    kill = metrics["kill_switch_active"]
    assert isinstance(kill, GaugeMetricFamily)
    assert kill.samples[0].value == 1.0


def test_kill_switch_inactive_when_no_row() -> None:
    collector = _build_collector(
        [
            ("FROM equity_snapshots", []),
            ("FROM decisions", []),
            ("FROM trades", []),
            ("FROM subagent_runs", []),
            ("FROM system_state", []),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}
    assert metrics["kill_switch_active"].samples[0].value == 0.0


def test_cycle_duration_histogram_buckets_match_duration_constants() -> None:
    duration_row = SimpleNamespace(
        total=3,
        sum_seconds=180.0,
        **{f"le_{i}": (1 if b >= 30 else 0) for i, b in enumerate(DURATION_BUCKETS)},
    )
    collector = _build_collector(
        [
            ("FROM equity_snapshots", [duration_row]),
            ("ORDER BY time DESC LIMIT 1", []),
            ("FROM decisions", []),
            ("FROM trades", []),
            ("FROM subagent_runs", []),
            ("FROM system_state", []),
        ],
    )
    metrics = {m.name: m for m in collector.collect()}
    duration = metrics["cycle_duration_seconds"]
    assert isinstance(duration, HistogramMetricFamily)
    bucket_samples = [s for s in duration.samples if s.name == "cycle_duration_seconds_bucket"]
    sum_sample = next(s for s in duration.samples if s.name == "cycle_duration_seconds_sum")
    count_sample = next(s for s in duration.samples if s.name == "cycle_duration_seconds_count")
    # +1 for the +Inf bucket appended in the collector.
    assert len(bucket_samples) == len(DURATION_BUCKETS) + 1
    assert sum_sample.value == 180.0
    assert count_sample.value == 3.0


def test_scrape_errors_increment_on_factory_failure() -> None:
    boom_factory = cast("Any", _raise_runtime_error)
    collector = CycleMetricsCollector(session_factory=boom_factory)
    metrics_first = list(collector.collect())
    assert any(m.name == "metrics_scrape_errors" for m in metrics_first)
    err1 = next(m for m in metrics_first if m.name == "metrics_scrape_errors")
    assert err1.samples[0].value == 1.0

    metrics_second = list(collector.collect())
    err2 = next(m for m in metrics_second if m.name == "metrics_scrape_errors")
    assert err2.samples[0].value == 2.0


def _raise_runtime_error() -> Any:
    msg = "no DB"
    raise RuntimeError(msg)


def test_agent_performance_gauges_emit_per_agent_rows() -> None:
    rows = [
        SimpleNamespace(
            agent_id="trading-agent",
            hit_rate_30d=0.72,
            sharpe_30d=1.4,
            pnl_30d=15.5,
            n_samples=20,
        ),
        SimpleNamespace(
            agent_id="news-bot",
            hit_rate_30d=None,
            sharpe_30d=None,
            pnl_30d=-3.2,
            n_samples=1,
        ),
    ]
    collector = _build_collector([("FROM agent_performance", rows)])
    metrics = {m.name: m for m in collector.collect()}

    hit = metrics["agent_hit_rate_30d"]
    assert isinstance(hit, GaugeMetricFamily)
    by_agent = {s.labels["agent_id"]: s.value for s in hit.samples}
    # Only the trading-agent row contributes — the news-bot has NULL hit_rate.
    assert by_agent == {"trading-agent": 0.72}

    sharpe = metrics["agent_sharpe_30d"]
    assert isinstance(sharpe, GaugeMetricFamily)
    assert {s.labels["agent_id"]: s.value for s in sharpe.samples} == {"trading-agent": 1.4}

    pnl = metrics["agent_pnl_30d_usd"]
    assert isinstance(pnl, GaugeMetricFamily)
    assert {s.labels["agent_id"]: s.value for s in pnl.samples} == {
        "trading-agent": 15.5,
        "news-bot": -3.2,
    }

    n_samples = metrics["agent_n_samples_30d"]
    assert isinstance(n_samples, GaugeMetricFamily)
    assert {s.labels["agent_id"]: s.value for s in n_samples.samples} == {
        "trading-agent": 20.0,
        "news-bot": 1.0,
    }


def test_agent_performance_gauges_empty_table() -> None:
    collector = _build_collector([])
    metrics = {m.name: m for m in collector.collect()}
    for name in (
        "agent_hit_rate_30d",
        "agent_sharpe_30d",
        "agent_pnl_30d_usd",
        "agent_n_samples_30d",
    ):
        family = metrics[name]
        assert isinstance(family, GaugeMetricFamily)
        assert family.samples == []
