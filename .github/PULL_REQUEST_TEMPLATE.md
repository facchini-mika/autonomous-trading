<!--
PR title format for plan-aligned work: `Phase N: <topic>`.
Never push directly to main. Never force-push. Never use --no-verify.
Single-operator project: branch protection enforces 4 CI gates and 0 required
approvals (GitHub forbids self-approval). Risk-sensitivity is enforced via
the AUDIT_LOG.md self-review pattern below.
-->

## Summary

<!-- One or two sentences: what changes and why. -->

## Risk-sensitive checklist

For every box checked, an `AUDIT_LOG.md` entry is **mandatory** and must
document: what changed, what could go wrong, why it's still safe.

- [ ] **`TRADING_MODE` flip?** (`paper` ↔ `real_capital`) — see `engineering.md §4`.
- [ ] **`MAX_CAPITAL_EUR` touched?** (any change, increase or decrease) — see `engineering.md §3`.
- [ ] **Touches `risk/**`?** — see `engineering.md §3`.
- [ ] **Strategy logic changed?** Document an explicit kill-criterion in this PR's description (when to roll back, what metric/threshold triggers it).

## Test plan

<!-- How was this verified? -->
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy --strict .`
- [ ] `uv run pytest` (Phase 3+)
- [ ] Manual verification steps:

## AUDIT_LOG link

<!-- Required if any risk-sensitive checkbox above is checked. -->
- AUDIT_LOG entry:
