# CLAUDE.md — AI-Coding Guard Rails

Authoritative for AI-driven edits in this repo. Short by design (per `engineering.md §4`).

## Architecture entry points

- `specs.md` — system overview + Mermaid architecture diagram.
- `engineering.md` — risk gates, safety controls, central settings, repo conventions, CI hooks, tech stack.
- `orchestration.md` — runtime topology, agent-team mechanics, capital allocation.
- `data_infrastructure.md` — schemas, prediction-market adapter, observability.
- `trading.md` — trading-runtime spec.
- `trading_feedback.md` — Tier-1 evaluation team.
- `optimization.md` — Tier-2 code-evaluation team and proposal governance.

When modifying behavior in a domain, **read the relevant spec file first**. Specs are the source of truth.

## Commands

```bash
# Install / sync deps
uv sync

# Lint + format
uv run ruff check .
uv run ruff format .

# Type-check (strict, hard CI gate per engineering.md §13)
uv run mypy --strict .

# Tests
uv run pytest
uv run pytest --cov=risk --cov-report=term-missing  # risk/ requires 100% coverage

# Local infra (Postgres + Timescale + Redis)
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml down

# DB migrations
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "<message>"
```

## Hard rules — NEVER

- **NEVER** commit real API keys, private keys, mnemonics, or `.env*` files. `.env` is gitignored.
- **NEVER** trigger live trades without explicit user confirmation in this session.
- **NEVER** modify code under `risk/` outside Plan Mode with explicit user approval (per `engineering.md §9`).
- **NEVER** push directly to `main` or use `--force` push.
- **NEVER** bypass risk gates by calling Polymarket adapter `place_order` directly — orders MUST flow through `risk/` enforcement.
- **NEVER** introduce numeric literals for tunables in `execution/`, `research/`, or `risk/` outside `shared/config/` (per `engineering.md §21`). Trivial constants (0, 1, 2) excepted.
- **NEVER** set `TRADING_MODE=real_capital` via env var or at runtime — only via settings-file PR with explicit reviewer approval (per `engineering.md §11`).

## Repo conventions

- **Phase 1 = local-first paper-mode validation.** AWS deployment is a future option, not the current target. Avoid Cloud-specific scaffolding (KMS, Secrets Manager, S3 SDK calls) unless explicitly requested.
- **Single source of truth for tunables:** `shared/config/settings.py` + `config/settings.toml` (per `engineering.md §21`).
- **Adapter pattern for external venues:** `shared/adapters/prediction_market.py` (Polymarket, Paper, Kalshi). Object store goes through `shared/adapters/object_store.py`.
- **Pydantic models for all typed artifacts** crossing task boundaries (per `engineering.md §13`).
- **Risk layer is protected.** All limits in `risk/` as plain constants/Settings imports — never AI outputs.
- **Branch naming:** `feat/<topic>`, `fix/<topic>`, `chore/<topic>`. Commit subject in imperative.
- **CLAUDE.local.md** is gitignored; personal notes only.

## Phase 1 deliberately NOT in scope

- AWS / KMS / Secrets Manager / CloudWatch / Sentry — deferred until paper-mode is validated.
- EIP-712 signing code — deferred until first `real_capital` switch.
- Multi-agent ensemble — deferred; Phase 1 ships a single-agent pipeline first.
- Prometheus / Grafana / Loki / Tempo stack — JSON-stdout logging is sufficient for now.
- Kalshi live integration — `KalshiAdapter` stays read-only.
- Backtest harness — out of scope per `engineering.md §11`; paper-mode replaces it.
