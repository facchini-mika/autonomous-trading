# First Cycle Runbook

Phase-5 operator playbook for the three cron jobs (`trading_cycle`,
`outcome_ingestion`, `lessons_summary`). The objective is **plumbing
verification**, not real trading — every cycle here runs against fake
or local fixtures. The first run against live Polymarket happens in
Phase 6 (`docs/operations/sandbox_smoke.md`).

## Prerequisites

| Requirement | Why | How |
|---|---|---|
| Docker Desktop running | Postgres 16 service container | `docker compose -f infra/docker-compose.yml up -d` |
| `.env` populated | per-role DSNs + container passwords | `cp .env.example .env` (dev defaults are fine) |
| `uv sync --frozen` | dependency hydration | `uv sync` |
| `uv run alembic upgrade head` | schema + GRANT matrix (incl. 0003) | runs once after Postgres is up |
| `claude` CLI on PATH | trading-cycle subagent dispatch | `npm install -g @anthropic-ai/claude-cli` (Phase 5b dependency) |
| `ANTHROPIC_API_KEY` in `.env` | `subagent_runner` headless calls | export from Anthropic console — only needed for `trading_cycle` |
| `MAX_CAPITAL_EUR=0` | hard-block real-money writes | already the default in `src/risk/capital_gate.py` — leave as-is |
| `TRADING_MODE=paper` | redirect place_order to `paper_trades` | already the default in `Settings` — leave as-is |

macOS note: `infra/scripts/run_cycle.sh` calls `timeout`. macOS lacks it
by default; install coreutils (`brew install coreutils`) and either
symlink `gtimeout` to `timeout` or set `TIMEOUT_BIN=gtimeout` in `.env`.

## Verifying each cycle

Three cycles, three commands. Each invokes the same wrapper used by cron.

### outcome_ingestion (no Claude required)

```
$ bash infra/scripts/run_cycle.sh outcome_ingestion
{"event": "outcome_ingestion_scanning", ...}
{"event": "outcome_ingestion_done", "counts": {...}, ...}
```

Inspect:

```
$ psql "$DATABASE_URL" -c "SELECT key, value FROM system_state WHERE key='last_outcome_ingestion_at'"
$ psql "$DATABASE_URL" -c "SELECT count(*) FROM predictions WHERE outcome IS NOT NULL"
```

### lessons_summary (no Claude required)

```
$ bash infra/scripts/run_cycle.sh lessons_summary
{"event": "lessons_summary_done", "inserted": 0, ...}
```

Inspect:

```
$ psql "$DATABASE_URL" -c "SELECT count(*), max(created_at) FROM lessons"
$ psql "$DATABASE_URL" -c "SELECT key, value FROM system_state WHERE key='last_lessons_summary_at'"
```

### trading_cycle (Claude required)

Trading cycle constructs the production adapter chain via
`shared.adapters.factory.make_adapter`. In paper mode that wraps a
`PolymarketAdapter` for reads — and `PolymarketAdapter.__init__` opens
the wallet file, even when no order is ever signed. So the trading-cycle
cron requires a wallet file at the path in
`Settings.KEY_PROVIDER_PATH` (default `~/.config/polymarket-trading/wallet.json`).

For Phase 5 verification you have two options:

1. **Skip locally — rely on the E2E test.** `tests/e2e/test_paper_cycle.py`
   exercises the full pipeline with `FakeAdapter` and `FakeGamma` and runs
   in CI on every PR. Local invocation of `trading_cycle` cron is not a
   Phase-5 exit requirement.
2. **Run against the real Claude/Polymarket side.** Generate a sandbox
   wallet via the Polymarket UI, encrypt it via a one-off script (not
   shipped — Phase 6 will add `infra/scripts/wallet_create.py`), and
   place it at `~/.config/polymarket-trading/wallet.json`. Then:

   ```
   $ ANTHROPIC_API_KEY=sk-... bash infra/scripts/run_cycle.sh trading_cycle
   ```

The first option is the recommended Phase-5 path. Move to option 2 only
when entering Phase 6.

## What gets written where

| Table | Written by | Per cycle |
|---|---|---|
| `markets` | `lead_bootstrap._persist_universe` | upsert by `market_id` |
| `market_snapshots` | `lead_bootstrap._persist_universe` | one row per market |
| `predictions` | `lead_bootstrap._persist_predictions_and_decisions` | one row per `Prediction` |
| `decisions` | same | one row per `Decision` |
| `paper_trades` | `PaperTradingAdapter.place_order` | one row per `action='trade'` |
| `cycle_plan` | `lead_bootstrap._persist_cycle_plan` | one new row, prior superseded |
| `system_state` | `outcome_ingestion`, `lessons_summary` | high-water-mark INSERT/UPDATE |
| `predictions.outcome` / `paper_trades.realized_pnl` | `outcome_ingestion` | UPDATE only after Gamma resolves |
| `lessons` | `lessons_summary` | one row per surprise |

`trades`, `positions`, `notes` are intentionally untouched by the MVP
trading cycle — those land later or are filled by future strategy
features.

## Pre-Phase-6 checklist

- [ ] Run `docs/operations/sandbox_smoke.md` against py-clob-client sandbox.
- [ ] AUDIT_LOG.md entry justifying the move from `paper` to `real_capital` (when that day comes).
- [ ] `MAX_CAPITAL_EUR` flip via 2-reviewer-equivalent PR + AUDIT_LOG entry.
- [ ] Branch protection: confirm `lint`, `type-check`, `gitleaks`, `trufflehog`, `pytest`, `import-linter`, `alembic-smoketest`, `e2e-paper-cycle` are all required status checks (operator step — see closing comment of Phase-5 PR for the `gh api` snippet).

## When something goes wrong

- **`InsufficientPrivilege` on `markets`/`system_state`**: migration 0003 not applied. Re-run `uv run alembic upgrade head`.
- **`ForeignKeyViolation` on `paper_trades.decision_id`**: Phase-4d ordering bug; fixed in Phase 5d. Confirm `git log src/execution/lead_bootstrap.py` shows the `_persist_predictions_and_decisions` split.
- **`could not determine data type of parameter`**: SQL parameter casting bug in `system_state` or `lessons` INSERTs; fixed in Phase 5d. Confirm `CAST(:v AS TEXT)` and `jsonb_build_array(...)` are present.
- **`structlog` not installed**: `uv sync` to refresh.
- **`claude: command not found`**: Phase-5b cron path requires the Claude CLI; `npm install -g @anthropic-ai/claude-cli`.
- **Cron logs in `/var/log/autonomous_trading/`**: ensure that directory exists and the cron user can write to it (paths in `infra/cron/*.cron` are placeholders — adjust per deployment).
