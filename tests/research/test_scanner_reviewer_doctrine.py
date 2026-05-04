"""Regression guard against hardcoding thresholds back into the doctrine.

The scanner-reviewer doctrine prompt at
``src/research/prompts/scanner_reviewer.md`` should drive its filter and
ranking thresholds from the ``ScannerThresholds`` payload the Lead
injects from ``Settings``. These tests assert that the doctrine
references those variables rather than the previous hardcoded literals
(100 USD, 6 h, 30 days, 0.10 spread).
"""

from __future__ import annotations

from pathlib import Path

DOCTRINE = Path(__file__).resolve().parents[2] / "src" / "research" / "prompts" / "scanner_reviewer.md"


def _doctrine_text() -> str:
    return DOCTRINE.read_text(encoding="utf-8")


def test_doctrine_references_threshold_payload() -> None:
    text = _doctrine_text()
    for token in (
        "thresholds.min_depth_1pct_usd",
        "thresholds.max_spread",
        "thresholds.min_ttr_hours",
        "thresholds.max_ttr_days",
        "thresholds.soon_resolve_threshold_days",
        "thresholds.soon_resolve_boost_multiplier",
    ):
        assert token in text, f"Doctrine missing reference to {token}"


def test_doctrine_does_not_hardcode_legacy_thresholds() -> None:
    text = _doctrine_text()
    # The previous doctrine hardcoded "< 100" (USD floor), "< 6 h", and "> 30 days".
    # If they reappear as bare literals in the filtering policy section we have regressed.
    forbidden = ["< 100`", "< 6 h", "> 30 days", "> 0.10`"]
    for token in forbidden:
        assert token not in text, f"Doctrine still hardcodes {token!r}"
