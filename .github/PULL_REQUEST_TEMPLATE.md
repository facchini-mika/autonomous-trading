<!--
PR title format for plan-aligned work: `Phase N: <topic>`.
Never push directly to main. Never force-push. Never use --no-verify.
-->

## Summary

<!-- One or two sentences: what changes and why. -->

## Risk-sensitive checklist

- [ ] **TRADING_MODE flip?** If yes: paper → real_capital requires ≥2 reviewer approvals **and** an `AUDIT_LOG.md` entry; real_capital → paper requires ≥1 reviewer + `AUDIT_LOG.md`. (See `engineering.md §4`.)
- [ ] **`MAX_CAPITAL_EUR` touched?** If yes: increases require ≥2 reviewer approvals; decreases require ≥1; both require an `AUDIT_LOG.md` entry. (See `engineering.md §3`.)
- [ ] **Touches `risk/**`?** If yes: ≥2 reviewer approvals (CODEOWNERS-enforced). Solo-operator: two distinct qualitative self-reviews + `AUDIT_LOG.md` entry.
- [ ] **Strategy logic changed?** If yes: document an explicit kill-criterion in this PR's description (when to roll back, what metric/threshold triggers it).

## Test plan

<!-- How was this verified? -->
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy --strict .`
- [ ] `uv run pytest` (Phase 3+)
- [ ] Manual verification steps:

## AUDIT_LOG link

<!-- Required if any risk-sensitive checkbox above is checked. -->
- AUDIT_LOG entry:
