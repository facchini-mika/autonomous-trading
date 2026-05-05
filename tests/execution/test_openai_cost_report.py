"""Tests for the per-cycle OpenAI cost-report CLI."""

from __future__ import annotations

import json
from decimal import Decimal

from execution.openai_cost_report import CycleRow, render_json, render_table


def _row(
    cycle_id: str | None = "cycle-A",
    *,
    calls: int = 1,
    failed_calls: int = 0,
    cost_usd: Decimal | None = None,
    searches: int = 1,
    input_tokens: int = 100,
    output_tokens: int = 50,
    unpriced_calls: int = 0,
) -> CycleRow:
    return CycleRow(
        cycle_id=cycle_id,
        calls=calls,
        failed_calls=failed_calls,
        cost_usd=cost_usd if cost_usd is not None else Decimal("0.123456"),
        searches=searches,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unpriced_calls=unpriced_calls,
    )


def test_render_table_empty() -> None:
    out = render_table([])
    assert "no web_search_calls match" in out


def test_render_table_sums_costs() -> None:
    rows = [
        _row(cycle_id="cycle-A", cost_usd=Decimal("1.000000"), searches=2),
        _row(cycle_id="cycle-B", cost_usd=Decimal("0.250000"), searches=1),
    ]
    out = render_table(rows)
    lines = out.splitlines()
    assert any("cycle-A" in line and "1.000000" in line for line in lines)
    assert any("cycle-B" in line and "0.250000" in line for line in lines)
    # Total row: 1.250000
    assert any(line.startswith("TOTAL") and "1.250000" in line for line in lines)


def test_render_table_flags_unpriced() -> None:
    rows = [_row(cycle_id="cycle-X", unpriced_calls=2)]
    out = render_table(rows)
    assert "2 successful call(s) had cost_usd=NULL" in out


def test_render_table_handles_null_cycle_id() -> None:
    rows = [_row(cycle_id=None)]
    out = render_table(rows)
    assert "(null)" in out


def test_render_json_serialises_decimal_as_string() -> None:
    rows = [
        _row(cycle_id="cycle-A", cost_usd=Decimal("0.500000")),
        _row(cycle_id="cycle-B", cost_usd=Decimal("0.123456")),
    ]
    payload = json.loads(render_json(rows))
    assert payload["total_cost_usd"] == "0.623456"
    assert payload["rows"][0]["cycle_id"] == "cycle-A"
    assert payload["rows"][0]["cost_usd"] == "0.500000"
    assert payload["rows"][1]["cost_usd"] == "0.123456"


def test_render_json_empty() -> None:
    payload = json.loads(render_json([]))
    assert payload["rows"] == []
    assert payload["total_cost_usd"] == "0"
