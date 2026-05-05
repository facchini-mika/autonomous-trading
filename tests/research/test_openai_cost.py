"""Unit tests for ``research.skills.openai_cost.calculate_cost_usd``.

Pure arithmetic — no DB, no settings env. We instantiate ``Settings()`` so
the test reads the same ``OPENAI_PRICING`` ClassVar the production path
does, then override individual prices via a small dataclass-like substitute
where it matters for an edge case.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from research.skills.openai_cost import UnknownModelError, calculate_cost_usd
from shared.config.settings import Settings


def _settings_with_pricing(pricing: dict[str, dict[str, Decimal]]) -> Settings:
    """Patch OPENAI_PRICING for one test via a SimpleNamespace shim.

    ``OPENAI_PRICING`` is a ClassVar on Settings, so mutating the live
    Settings would leak across tests. The cost helper only reads
    ``settings.OPENAI_PRICING`` so a duck-typed shim is enough.
    """
    return cast("Settings", SimpleNamespace(OPENAI_PRICING=pricing, OPENAI_MODEL="gpt-test"))


def test_zero_usage_yields_zero_cost() -> None:
    settings = Settings()
    cost = calculate_cost_usd(
        model="gpt-5.5",
        input_tokens=0,
        output_tokens=0,
        cached_input_tokens=0,
        web_search_count=0,
        settings=settings,
    )
    assert cost == Decimal(0)


def test_each_axis_priced_independently() -> None:
    pricing = {
        "gpt-test": {
            "input_per_1m_usd": Decimal("3.00"),
            "output_per_1m_usd": Decimal("6.00"),
            "cached_input_per_1m_usd": Decimal("1.00"),
            "web_search_per_1k_usd": Decimal("25.00"),
        },
    }
    settings = _settings_with_pricing(pricing)
    cost = calculate_cost_usd(
        model="gpt-test",
        input_tokens=1_000_000,  # 800k billable + 200k cached
        output_tokens=500_000,
        cached_input_tokens=200_000,
        web_search_count=4,
        settings=settings,
    )
    expected = (
        Decimal(800_000) * Decimal("3.00") / 1_000_000  # 2.40
        + Decimal(200_000) * Decimal("1.00") / 1_000_000  # 0.20
        + Decimal(500_000) * Decimal("6.00") / 1_000_000  # 3.00
        + Decimal(4) * Decimal("25.00") / 1_000  # 0.10
    )
    assert cost == expected


def test_cached_clamped_to_input_total() -> None:
    """If a buggy SDK reports more cached than input, treat all input as cached."""
    pricing = {
        "gpt-test": {
            "input_per_1m_usd": Decimal("100.00"),
            "output_per_1m_usd": Decimal(0),
            "cached_input_per_1m_usd": Decimal("1.00"),
            "web_search_per_1k_usd": Decimal(0),
        },
    }
    settings = _settings_with_pricing(pricing)
    cost = calculate_cost_usd(
        model="gpt-test",
        input_tokens=1_000_000,
        output_tokens=0,
        cached_input_tokens=2_000_000,  # bogus
        web_search_count=0,
        settings=settings,
    )
    # All 1M tokens billed at the cached rate (1.00/M = 1.00), none at 100.00/M.
    assert cost == Decimal("1.00")


def test_negative_inputs_treated_as_zero() -> None:
    settings = Settings()
    cost = calculate_cost_usd(
        model="gpt-5.5",
        input_tokens=-100,
        output_tokens=-50,
        cached_input_tokens=-10,
        web_search_count=-1,
        settings=settings,
    )
    assert cost == Decimal(0)


def test_unknown_model_raises() -> None:
    settings = Settings()
    with pytest.raises(UnknownModelError):
        calculate_cost_usd(
            model="gpt-unknown-99",
            input_tokens=1,
            output_tokens=1,
            cached_input_tokens=0,
            web_search_count=0,
            settings=settings,
        )


def test_default_pricing_table_includes_configured_model() -> None:
    """The model named in settings.OPENAI_MODEL must have a pricing entry,
    otherwise every web_search call would log ``openai_pricing_missing``."""
    settings = Settings()
    assert settings.OPENAI_MODEL in settings.OPENAI_PRICING, (
        f"settings.OPENAI_MODEL={settings.OPENAI_MODEL!r} has no entry in OPENAI_PRICING"
    )
