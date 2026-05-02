"""Surprise heuristic — the gating function for what becomes a lesson.

A prediction is "surprising" if either:
  1. The probability assigned was far from the resolved outcome
     (`abs(p_yes - outcome_as_int) > Settings.SURPRISE_THRESHOLD`).
  2. The realised PnL fell outside the expected band derived from the
     prediction's edge (default band = `2 * |edge| * notional`).

Pure functions only — no I/O, no LLM. Property-tested in Phase 4c.
"""

from __future__ import annotations


def is_surprise(
    *,
    p_yes: float,
    outcome_yes: bool,
    realized_pnl: float | None = None,
    notional: float = 0.0,
    edge: float = 0.0,
    threshold: float,
    pnl_band_factor: float = 2.0,
) -> bool:
    """Return True iff this prediction is surprising enough to be a lesson.

    `threshold` is `Settings.SURPRISE_THRESHOLD` (decimal, e.g. 0.3).
    `pnl_band_factor` controls the realized-PnL outlier sensitivity; default 2.
    """
    if not 0.0 <= p_yes <= 1.0:
        msg = "p_yes must be in [0, 1]"
        raise ValueError(msg)
    if threshold < 0.0:
        msg = "threshold must be non-negative"
        raise ValueError(msg)

    outcome_int = 1.0 if outcome_yes else 0.0
    if abs(p_yes - outcome_int) > threshold:
        return True

    if realized_pnl is not None and notional > 0.0:
        band = pnl_band_factor * abs(edge) * notional
        if abs(realized_pnl) > band:
            return True

    return False


P_YES_NEUTRAL = 0.5


def categorize(p_yes: float, *, outcome_yes: bool, threshold: float) -> str:
    """Cheap label for the lesson template ('overconfident_yes', etc.)."""
    if abs(p_yes - (1.0 if outcome_yes else 0.0)) <= threshold:
        return "expected"
    if outcome_yes and p_yes < P_YES_NEUTRAL:
        return "missed_yes"
    if not outcome_yes and p_yes > P_YES_NEUTRAL:
        return "missed_no"
    if outcome_yes:
        return "overconfident_yes"
    return "overconfident_no"
