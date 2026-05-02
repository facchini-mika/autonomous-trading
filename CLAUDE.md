# CLAUDE.md

## Project pointer
Architecture source-of-truth: `specs/specs.md` + 6 sub-files
(`specs/trading.md`, `specs/engineering.md`, `specs/data_infrastructure.md`,
 `specs/orchestration.md`, `specs/trading_feedback.md`, `specs/optimization.md`).
Read `specs/engineering.md` first. Build roadmap: `plan.md`.

## Commands
- `uv sync` — install/refresh deps
- `uv run pytest` — tests
- `uv run ruff check .` / `uv run ruff format .` — lint + format
- `uv run mypy --strict .` — types
- `uv run pre-commit run --all-files` — full pre-commit sweep
- `uv run alembic upgrade head` — DB migrations (Phase 3+)

## No-go list
- NEVER commit real API keys, mnemonics, signing keys, or `.env*` files.
- NEVER trigger live (`real_capital`) trades without explicit user confirmation in this session.
- NEVER modify code under `src/risk/**` outside Plan Mode with prior user approval.
- NEVER push directly to `main`. NEVER force-push. NEVER use `--no-verify`.
- NEVER introduce numeric tunables outside `src/shared/config/settings.py` (allow-list: 0, 1, 2). Phase 3+.

## Default trading mode
Default `TRADING_MODE` is `paper`. A clean checkout cannot trade real capital
without an explicit settings change. Switching `paper -> real_capital` requires
a PR with an `AUDIT_LOG.md` entry that documents the operator's safety review
(see "Reviewer rule" below). See `specs/engineering.md §4`.

## Reviewer rule
This is a **single-operator project**. There is exactly one human, who is
both author and reviewer. GitHub forbids self-approving one's own PR, so the
platform-enforced approval count for normal PRs is **0**; the four CI gates
(`lint`, `type-check`, `gitleaks`, `trufflehog`) are the merge prerequisites.

Risk-sensitive PRs — any change touching `src/risk/**`, the `MAX_CAPITAL_EUR`
constant, or a `TRADING_MODE` flip — additionally require an `AUDIT_LOG.md`
entry that documents the operator's safety review (what was changed, what
could go wrong, why it's still safe). The audit-log entry is the second-
review trail; CI cannot enforce it, the operator's discipline does. If the
project ever gains a second human operator, raise `required_approving_review_count`
in branch protection back to ≥1. See `specs/engineering.md §3, §4, §8`.

## Repo layout
See `specs/engineering.md §5` for the canonical tree:
`src/research/`, `src/execution/`, `src/risk/`, `src/shared/{config,adapters,models}/`,
`infra/`, `alembic/`, `tests/`, `docs/`, `.claude/`.
`src/risk/` is protected code — see Plan Mode requirement below.

## Settings discipline
All numeric thresholds, limits, parameters, and tunables live in
`src/shared/config/settings.py` (Phase 3+). Never duplicate, never hardcode in
`src/risk/`, `src/execution/`, or `src/research/`. See `specs/engineering.md §10`.

## Plan Mode requirement
Edits under `src/risk/**` only in Plan Mode with explicit prior user approval.
The `specs/engineering.md §9` PreToolUse hook enforces this from Phase 2+.

## Branch workflow
Always feature-branch + PR. Never push to `main`. Never `--force`. Never
`--no-verify`. PR title format: `Phase N: <topic>` for plan-aligned work.

## Local notes
Personal/transient notes go in `CLAUDE.local.md` (gitignored). Do not commit it.
