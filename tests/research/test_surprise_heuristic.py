"""Property-based tests for the surprise heuristic."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from research.surprise_heuristic import categorize, is_surprise

P = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
THRESHOLD = st.floats(min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False)


@given(p_yes=P, threshold=THRESHOLD)
def test_perfect_prediction_is_never_surprising(p_yes: float, threshold: float) -> None:
    outcome = p_yes >= 0.5
    delta = abs(p_yes - (1.0 if outcome else 0.0))
    surprising = is_surprise(p_yes=p_yes, outcome_yes=outcome, threshold=threshold)
    if delta <= threshold:
        assert not surprising


@given(threshold=THRESHOLD)
def test_extreme_miss_yes_is_surprising(threshold: float) -> None:
    if threshold >= 1.0:
        return
    assert is_surprise(p_yes=0.05, outcome_yes=True, threshold=threshold)


@given(threshold=THRESHOLD)
def test_extreme_miss_no_is_surprising(threshold: float) -> None:
    if threshold >= 1.0:
        return
    assert is_surprise(p_yes=0.95, outcome_yes=False, threshold=threshold)


def test_realized_pnl_outlier_triggers() -> None:
    assert is_surprise(
        p_yes=0.5,
        outcome_yes=True,
        realized_pnl=100.0,
        notional=10.0,
        edge=0.05,
        threshold=0.5,
    )


def test_realized_pnl_within_band_does_not_trigger() -> None:
    assert not is_surprise(
        p_yes=0.5,
        outcome_yes=True,
        realized_pnl=0.5,
        notional=10.0,
        edge=0.05,
        threshold=0.5,
    )


def test_invalid_p_yes_rejected() -> None:
    with pytest.raises(ValueError, match="p_yes"):
        is_surprise(p_yes=1.5, outcome_yes=True, threshold=0.3)


def test_invalid_threshold_rejected() -> None:
    with pytest.raises(ValueError, match="threshold"):
        is_surprise(p_yes=0.5, outcome_yes=True, threshold=-0.1)


def test_threshold_zero_means_anything_surprises() -> None:
    assert is_surprise(p_yes=0.5, outcome_yes=True, threshold=0.0)


def test_categorize_expected_when_close() -> None:
    assert categorize(0.95, outcome_yes=True, threshold=0.1) == "expected"


def test_categorize_missed_yes() -> None:
    assert categorize(0.2, outcome_yes=True, threshold=0.1) == "missed_yes"


def test_categorize_missed_no() -> None:
    assert categorize(0.8, outcome_yes=False, threshold=0.1) == "missed_no"


def test_categorize_overconfident_yes() -> None:
    # p=0.6, outcome=YES, threshold=0.1 → |0.6 - 1.0| = 0.4 > 0.1, p > 0.5, outcome yes
    assert categorize(0.6, outcome_yes=True, threshold=0.1) == "overconfident_yes"


def test_categorize_overconfident_no() -> None:
    assert categorize(0.4, outcome_yes=False, threshold=0.1) == "overconfident_no"
