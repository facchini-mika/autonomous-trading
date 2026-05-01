# CLAUDE.md

## Project pointer
Architecture source-of-truth: `specs.md` + 6 sub-files
(`trading.md`, `engineering.md`, `data_infrastructure.md`,
 `orchestration.md`, `trading_feedback.md`, `optimization.md`).
Read `engineering.md` first. Build roadmap: `plan.md`.

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
- NEVER modify code under `risk/**` outside Plan Mode with prior user approval.
- NEVER push directly to `main`. NEVER force-push. NEVER use `--no-verify`.
- NEVER introduce numeric tunables outside `shared/config/settings.py` (allow-list: 0, 1, 2). Phase 3+.

## Default trading mode
Default `TRADING_MODE` is `paper`. A clean checkout cannot trade real capital
without an explicit settings change. Switching `paper -> real_capital` requires
a PR with >=2 reviewer approvals and an `AUDIT_LOG.md` entry. See `engineering.md §4`.

## Reviewer rule
Any change touching `risk/**` or the `MAX_CAPITAL_EUR` constant requires
>=2 human reviewer approvals on the PR (enforced by `CODEOWNERS` + branch
protection from Phase 1+) plus an `AUDIT_LOG.md` entry. Solo-operator pattern:
self-approve twice with two distinct qualitative reviews. See `engineering.md §3, §8`.

## Repo layout
See `engineering.md §5` for the canonical tree:
`research/`, `execution/`, `risk/`, `shared/{config,adapters,models}/`,
`infra/`, `alembic/`, `tests/`, `docs/`, `.claude/`.
`risk/` is protected code — see Plan Mode requirement below.

## Settings discipline
All numeric thresholds, limits, parameters, and tunables live in
`shared/config/settings.py` (Phase 3+). Never duplicate, never hardcode in
`risk/`, `execution/`, or `research/`. See `engineering.md §10`.

## Plan Mode requirement
Edits under `risk/**` only in Plan Mode with explicit prior user approval.
The `engineering.md §9` PreToolUse hook enforces this from Phase 2+.

## Branch workflow
Always feature-branch + PR. Never push to `main`. Never `--force`. Never
`--no-verify`. PR title format: `Phase N: <topic>` for plan-aligned work.

## Local notes
Personal/transient notes go in `CLAUDE.local.md` (gitignored). Do not commit it.
