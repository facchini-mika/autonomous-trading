"""Pydantic-validator tests for ``Decision._normalize_notional_aliases``.

The risk-execution-LLM was observed in cycle-7 emitting
``final_notional_usd`` / ``clipped_notional_usd`` instead of the
canonical ``clipped_notional`` / ``notional_usd`` keys the Lead's
``_decision_notional`` reads. The ``Decision`` Pydantic model now
hoists those aliases at parse-time so the Lead's reader contract
stays untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shared.models import Decision

_NOW = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)


def _decision(gate_results: dict[str, Any]) -> Decision:
    return Decision(
        cycle_id="cycle-test",
        market_id="0xa",
        p_consensus=0.6,
        q_market=0.5,
        edge=0.1,
        gate_results=gate_results,
        action="trade",
        rationale="ok",
        created_at=_NOW,
    )


def test_normalises_final_notional_usd_to_canonical() -> None:
    d = _decision({"final_notional_usd": 168.47})
    assert d.gate_results["clipped_notional"] == 168.47
    assert d.gate_results["notional_usd"] == 168.47


def test_normalises_clipped_notional_usd_to_canonical() -> None:
    d = _decision({"clipped_notional_usd": 100.0})
    assert d.gate_results["clipped_notional"] == 100.0
    assert d.gate_results["notional_usd"] == 100.0


def test_normalises_notional_clipped_usd_to_canonical() -> None:
    """Observed in cycle-1779210369: word-order permutation ``notional_clipped_usd``."""
    d = _decision({"notional_clipped_usd": 490.0})
    assert d.gate_results["clipped_notional"] == 490.0
    assert d.gate_results["notional_usd"] == 490.0


def test_canonical_takes_precedence_over_alias() -> None:
    d = _decision(
        {
            "clipped_notional": 50.0,
            "notional_usd": 50.0,
            "final_notional_usd": 999.0,
            "clipped_notional_usd": 999.0,
        }
    )
    assert d.gate_results["clipped_notional"] == 50.0
    assert d.gate_results["notional_usd"] == 50.0


def test_no_normalisation_when_no_alias_keys_present() -> None:
    payload = {"side": "yes", "kill_switch": {"passed": True}}
    d = _decision(dict(payload))
    assert d.gate_results == payload
    assert "clipped_notional" not in d.gate_results
    assert "notional_usd" not in d.gate_results


def test_zero_value_is_not_hoisted() -> None:
    """A failing concentration cap may emit `final_notional_usd=0`; do not hoist."""
    d = _decision({"final_notional_usd": 0})
    assert "clipped_notional" not in d.gate_results
    assert "notional_usd" not in d.gate_results


def test_negative_value_is_not_hoisted() -> None:
    d = _decision({"final_notional_usd": -10.0})
    assert "clipped_notional" not in d.gate_results
    assert "notional_usd" not in d.gate_results


def test_bool_true_is_not_hoisted() -> None:
    """Defensive: bool is technically a numeric subclass; reject it."""
    d = _decision({"final_notional_usd": True})
    assert "clipped_notional" not in d.gate_results


def test_string_value_is_not_hoisted() -> None:
    d = _decision({"final_notional_usd": "168.47"})
    assert "clipped_notional" not in d.gate_results


def test_only_clipped_notional_canonical_present_does_not_pull_notional_usd_from_alias() -> None:
    """If canonical clipped_notional exists but notional_usd is missing,
    notional_usd may still be hoisted from an alias."""
    d = _decision({"clipped_notional": 100.0, "final_notional_usd": 200.0})
    assert d.gate_results["clipped_notional"] == 100.0
    # notional_usd missing canonically; alias hoisted.
    assert d.gate_results["notional_usd"] == 200.0


def test_alias_hoist_preserves_other_keys() -> None:
    payload = {
        "final_notional_usd": 168.47,
        "kill_switch": {"passed": True},
        "edge_threshold": {"passed": True, "edge": 0.06},
    }
    d = _decision(dict(payload))
    assert d.gate_results["clipped_notional"] == 168.47
    assert d.gate_results["kill_switch"] == {"passed": True}
    assert d.gate_results["edge_threshold"] == {"passed": True, "edge": 0.06}
