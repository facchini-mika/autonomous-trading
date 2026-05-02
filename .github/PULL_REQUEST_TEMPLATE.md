<!--
PR title format for plan-aligned work: `Phase N: <topic>`.
Never push directly to main. Never force-push. Never use --no-verify.
Single-operator project: branch protection enforces 4 CI gates and 0 required
approvals (GitHub forbids self-approval). Risk-sensitivity is enforced via
the AUDIT_LOG.md self-review pattern below.
-->

## Summary

<!-- One or two sentences: what changes and why. -->

## Risk-sensitive review

Replace each **TBD** with **Yes** or **No**. If any answer is **Yes**, an
`AUDIT_LOG.md` entry in this PR is mandatory and must document: what
changed, what could go wrong, why it's still safe.

- `TRADING_MODE` flip (`paper` ↔ `real_capital`)? **TBD** — see `specs/engineering.md §4`.
- `MAX_CAPITAL_EUR` touched (any direction)? **TBD** — see `specs/engineering.md §3`.
- Touches `risk/**`? **TBD** — see `specs/engineering.md §3`.
- Strategy logic changed? **TBD** — if Yes, document the kill-criterion in **Summary** above (when to roll back, what metric/threshold triggers it).

## Test plan

Local checks (run before opening this PR — tick when done):

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy --strict .`
- [ ] `uv run pytest` (Phase 3+)
- [ ] Manual verification:

> CI status checks (`lint`, `type-check`, `gitleaks`, `trufflehog`) are
> enforced by branch protection — no manual checkbox needed; GitHub
> blocks merge until all four are green.

## AUDIT_LOG link

If any **Yes** above, link the entry:

- AUDIT_LOG entry:
