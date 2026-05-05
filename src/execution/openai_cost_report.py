"""Per-cycle OpenAI web_search cost report.

Aggregates ``web_search_calls`` rows into per-``cycle_id`` totals. The
parallel Anthropic cost report sums ``subagent_runs.cost_usd``; this
companion script adds the OpenAI Responses-API spend that is invisible
to that path. Together they answer "what did one cycle cost?".

Usage::

    uv run python -m execution.openai_cost_report --cycle <cycle_id>
    uv run python -m execution.openai_cost_report --since 2026-05-01
    uv run python -m execution.openai_cost_report --since 2026-05-01 --json

The script reads from the ``trading_cycle`` DB role because that is the
only role with a SELECT grant on ``web_search_calls`` today (see
migration 0004). It writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import text as sql_text

from shared.db import get_session

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session


_AGGREGATE_SQL = sql_text(
    """
    SELECT
        cycle_id,
        COUNT(*) FILTER (WHERE error_type IS NULL)                AS calls,
        COUNT(*) FILTER (WHERE error_type IS NOT NULL)            AS failed_calls,
        COALESCE(SUM(cost_usd) FILTER (WHERE error_type IS NULL), 0) AS cost_usd,
        COALESCE(SUM(web_search_count) FILTER (WHERE error_type IS NULL), 0) AS searches,
        COALESCE(SUM(input_tokens) FILTER (WHERE error_type IS NULL), 0)  AS input_tokens,
        COALESCE(SUM(output_tokens) FILTER (WHERE error_type IS NULL), 0) AS output_tokens,
        COUNT(*) FILTER (WHERE error_type IS NULL AND cost_usd IS NULL)
                                                                  AS unpriced_calls
    FROM web_search_calls
    WHERE (CAST(:cycle_id AS TEXT) IS NULL OR cycle_id = CAST(:cycle_id AS TEXT))
      AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR started_at >= CAST(:since AS TIMESTAMPTZ))
    GROUP BY cycle_id
    ORDER BY cycle_id NULLS LAST
    """
)


@dataclass(frozen=True)
class CycleRow:
    cycle_id: str | None
    calls: int
    failed_calls: int
    cost_usd: Decimal
    searches: int
    input_tokens: int
    output_tokens: int
    unpriced_calls: int


def fetch_rows(
    session: Session,
    *,
    cycle_id: str | None,
    since: datetime | None,
) -> list[CycleRow]:
    rs = session.execute(
        _AGGREGATE_SQL,
        {"cycle_id": cycle_id, "since": since},
    )
    return [
        CycleRow(
            cycle_id=row.cycle_id,
            calls=int(row.calls),
            failed_calls=int(row.failed_calls),
            cost_usd=Decimal(row.cost_usd),
            searches=int(row.searches),
            input_tokens=int(row.input_tokens),
            output_tokens=int(row.output_tokens),
            unpriced_calls=int(row.unpriced_calls),
        )
        for row in rs
    ]


def render_table(rows: Sequence[CycleRow]) -> str:
    if not rows:
        return "no web_search_calls match the filter\n"

    header = (
        f"{'cycle_id':<32}  {'calls':>5}  {'failed':>6}  "
        f"{'searches':>8}  {'in_tok':>8}  {'out_tok':>8}  {'cost_usd':>10}"
    )
    sep = "-" * len(header)
    lines = [header, sep]

    total_cost = Decimal(0)
    total_calls = 0
    total_failed = 0
    total_searches = 0
    total_input = 0
    total_output = 0
    total_unpriced = 0

    for row in rows:
        cid_display = row.cycle_id if row.cycle_id is not None else "(null)"
        lines.append(
            f"{cid_display:<32}  {row.calls:>5}  {row.failed_calls:>6}  "
            f"{row.searches:>8}  {row.input_tokens:>8}  {row.output_tokens:>8}  "
            f"{row.cost_usd:>10.6f}"
        )
        total_cost += row.cost_usd
        total_calls += row.calls
        total_failed += row.failed_calls
        total_searches += row.searches
        total_input += row.input_tokens
        total_output += row.output_tokens
        total_unpriced += row.unpriced_calls

    lines.append(sep)
    lines.append(
        f"{'TOTAL':<32}  {total_calls:>5}  {total_failed:>6}  "
        f"{total_searches:>8}  {total_input:>8}  {total_output:>8}  "
        f"{total_cost:>10.6f}"
    )
    if total_unpriced:
        lines.append(
            f"\nNOTE: {total_unpriced} successful call(s) had cost_usd=NULL "
            "(model missing from settings.OPENAI_PRICING) — totals exclude them."
        )
    return "\n".join(lines) + "\n"


def render_json(rows: Sequence[CycleRow]) -> str:
    payload = {
        "rows": [
            {
                "cycle_id": row.cycle_id,
                "calls": row.calls,
                "failed_calls": row.failed_calls,
                "searches": row.searches,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cost_usd": str(row.cost_usd),
                "unpriced_calls": row.unpriced_calls,
            }
            for row in rows
        ],
        "total_cost_usd": str(sum((r.cost_usd for r in rows), Decimal(0))),
    }
    return json.dumps(payload, indent=2) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openai_cost_report",
        description="Aggregate OpenAI web_search costs per cycle.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cycle", help="filter to a single cycle_id")
    group.add_argument(
        "--since",
        type=date.fromisoformat,
        help="lower bound on started_at (YYYY-MM-DD, UTC)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    since_dt = datetime.combine(args.since, datetime.min.time(), tzinfo=UTC) if args.since is not None else None

    with get_session("trading_cycle") as session:
        rows = fetch_rows(session, cycle_id=args.cycle, since=since_dt)

    output = render_json(rows) if args.json else render_table(rows)
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":  # pragma: no cover — module entry, exercised via tests
    sys.exit(main())
