# Engineering — Risk, Safety, Repository Conventions, AI-Coding Workflow

How humans + AI build, modify, and operate the system. Owns the deterministic risk gates, kill-switch, audit-trail wiring, repository conventions, secret handling, mode-flag governance, central settings module, CI/branch-protection, and the reference tech stack.

**What lives here:** values, governance, deterministic guard rails, and the engineering scaffolding to build the repo. **What does not live here:** what the bot does (`trading.md`, `specs.md`), runtime topology (`orchestration.md`), schemas + adapters + sources (`data_infrastructure.md`), Tier-1 evaluation behavior (`trading_feedback.md`), Tier-2 evaluation behavior (`optimization.md`).

---

## MVP scope (engineering only)

Build a minimum repo that runs the prototype described in `specs.md` + `trading.md`. The engineering decisions for that build are:

- **Single language:** Python 3.12, `uv`-managed.
- **Single storage tier:** Postgres 16 only (per `data_infrastructure.md` MVP scope). No Redis, no TimescaleDB, no S3, no Vault, no Secrets Manager, no KMS.
- **Single deployment target:** local. `docker-compose` for the Postgres container; the trading process runs as a regular Python script triggered by `cron` (or `/loop`). No k8s, no ECS, no Agent-Teams production runtime.
- **Strict CI gates from day 1:** `mypy --strict`, `ruff`, `pytest`, `gitleaks`. Risk-layer 100% coverage hard gate.
- **Risk + safety enforcement is non-negotiable** even in MVP — the §1 gates, the §3 capital cap, the §4 mode flag, and the §9 hooks are all in scope from commit one.
- **Everything that does not directly serve the prototype is deferred** — listed in §13 *Weiterer Ausbau (post-MVP)*.

---

## 1. Risk Management

Three deterministic gates plus the constitutional capital cap. All limits are plain constants in `risk/`, never AI outputs.

**Per-trade gates** (applied in order; trade rejected on first failure):
1. **Concentration** — proposed notional ≤ **15% of equity** in any single market.
2. **Solvency** — paper-cash ≥ proposed notional + estimated fees + open-order reservations.
3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap (default 25% of equity, central setting §10).

**Constitutional cap (§3).** `MAX_CAPITAL_EUR` is a hard, two-human-approval-only ceiling on gross deployed capital. Independent of all other gates. In MVP this is the paper-cash budget; on the `paper → real_capital` switch it becomes the real-USDC ceiling.

**Manual kill-switch (§2).** Operator halts all new orders by flipping a row in `system_state` (`data_infrastructure.md §1`). No automatic drawdown trip-wires in MVP — drawdown is monitored, the operator decides whether to flip.

---

## 2. Safety & Controls

**Kill switch:**
- A row in `system_state(key='kill_switch', value=true)` (Postgres).
- The execution-engine reads it before every order; `true` halts new orders. Existing positions stay open.
- **Manual trigger only** — no automatic trip-wires in MVP. The constitutional `MAX_CAPITAL_EUR` (§3) is the only fully-automatic guard.

**Manual override (CLI in MVP):** pause/resume the cycle, close a paper-position, edit allocation, flip the kill switch.

**Sanity gates** (block before submission, in addition to §1):
- Order size > 50% of equity → block.
- Order price ∉ (0.005, 0.995) → block.
- > 100 orders / hour → throttle.
- > 50 simultaneous open positions → require manual approval.

**Audit-trail wiring.** Every cycle is replayable from Postgres alone. The shape — schemas, retention, observability surfaces — is owned by `data_infrastructure.md §1`–§3. Engineering's contribution is two enforcement rules:
- The `decisions` row records the gate evaluations and chosen action; the `predictions.inference_log` JSONB carries prompt + Claude output + tool calls + `web_search` results.
- Append-only by convention. In `real_capital` mode, the DB role used by the trading process has no DELETE on `decisions`, `predictions`, `trades`, or `paper_trades`.

---

## 3. Risk Layer Protection + Capital Gate

`risk/` owns the deterministic guard rails. The AI decides *what* to trade; the risk layer decides *whether and how much*.

- All §1 limits = plain constants/Settings imports in `risk/`, never AI outputs.
- AI agents may not edit `risk/` outside Plan Mode + explicit user approval (§9 hook).
- All trading-decision paths import from `risk/`. Direct order-adapter calls bypassing `risk/` are forbidden by `import-linter` in CI.
- `risk/` requires 100% line coverage; CI fails below.

**Capital gate.** Single constant:

```python
# risk/capital_gate.py
MAX_CAPITAL_EUR: Final = <TBD_BY_OPERATOR>
```

- The order-submission path rejects any order pushing gross deployed capital above `MAX_CAPITAL_EUR`.
- Constant changed only via PR; in `real_capital` mode this requires ≥ 2 reviewer approvals + an audit-log entry. Decreases also gated to ≥ 1 reviewer.

---

## 4. Operational Modes (paper vs. real_capital) — governance

The flag itself lives in the central settings module (§10):

```
TRADING_MODE = "paper" | "real_capital"
```

Engineering owns the **governance** of this flag — what the runtime does with it is in `trading.md` and `data_infrastructure.md §2`.

- **Default `paper`.** A clean checkout cannot trade real capital without an explicit settings-file change.
- **Switching is manual.** PR with ≥ 1 reviewer approval (real → paper, defensive direction) or ≥ 2 reviewer approvals (paper → real_capital, offensive direction); audit-log entry on merge. **Never via env var, never at runtime.**
- **No backtest harness.** Polymarket markets are too short-lived; paper-mode is the validation gate (`trading_feedback.md §7` owns the promotion criteria).

---

## 5. Repository Layout

```
autonomous_trading/
├── research/        # agent prompt + skills (web_search, etc.)
├── execution/       # paper-trade ledger; later, CLOB client
├── risk/            # limits, kill switch, sanity gates, capital_gate (§3)
├── shared/
│   ├── config/      # central settings (§10)
│   ├── adapters/    # PredictionMarketAdapter (Polymarket + Paper)
│   └── models/      # Pydantic models for every typed artifact
├── infra/           # docker-compose (Postgres only in MVP), scripts/
├── alembic/         # DB migrations
├── tests/
├── docs/
├── .claude/         # project-scoped Claude Code config (committed)
├── CLAUDE.md        # AI-coding guard rails (committed)
├── CLAUDE.local.md  # personal notes (gitignored)
└── *.md             # specs (this file + 6 others, plus specs.md)
```

`risk/` is *protected code* — see §3.

---

## 6. CLAUDE.md (project)

Short, high-signal, < 200 lines. Every line must answer *yes* to: "Would Claude make a mistake without this line?"

Mandatory:
- Build/test/lint commands (`pytest`, `ruff`, `mypy --strict`, `alembic upgrade head`).
- No-go list:
  - **NEVER** commit real API keys, mnemonics, `.env*`.
  - **NEVER** trigger live trades without explicit user confirmation in this session.
  - **NEVER** modify code under `risk/` outside Plan Mode with explicit approval.
  - **NEVER** push directly to `main` or force-push.
- Pointer to `specs.md` and the 6 component spec files.

Personal/transient → `CLAUDE.local.md` (gitignored).

---

## 7. Secret Management (MVP)

- `.env*` in `.gitignore`. `gitleaks` + `trufflehog` in `.pre-commit-config.yaml` and on every PR.
- API tokens (Anthropic, OpenAI, Polymarket-read) live in `.env` for MVP — local `.env` is sufficient because we never trade real capital here.
- **No private keys in MVP.** EIP-712 signing arrives only on the `real_capital` switch; cloud KMS or hardware key (YubiHSM) ship with that change, not before.

---

## 8. GitHub Integration (MVP)

- **`gh` CLI** required locally — token-cheaper for AI use.
- **Branch protection on `main`:**
  - Required PR review (≥ 1 human reviewer; **≥ 2 humans for any change touching `risk/`** or `MAX_CAPITAL_EUR`).
  - Required status checks: `pytest`, `ruff`, `mypy --strict`, `gitleaks`.
  - No direct pushes, no force-push.
- Solo operator note: the operator is both author and reviewer for MVP. The ≥ 2-human rule on `risk/` is a placeholder enforced by branch protection — the operator self-approves twice with two distinct reviews and an audit-log note. Treated as a real gate, not a formality.

---

## 9. Hooks (`.claude/settings.json`) — MVP set

Hooks are deterministic guarantees. CLAUDE.md is a request, hooks are enforcement.

| Hook | Trigger | Action |
|---|---|---|
| `PostToolUse` Edit/Write | After code edit | `ruff` + `mypy --strict` + relevant `pytest` subset; block on failure |
| `PreToolUse` Bash | Before bash | Block `rm -rf`, `git push --force`, `git reset --hard`; block writes to `.env*` |
| `PreToolUse` Edit/Write on `risk/**` | Before risk-code edit | Block unless session in Plan Mode with prior user approval |
| `Stop` | Before turn end | `gitleaks` on staged diff; abort on any secret hit |
| `UserPromptSubmit` | On user prompt | If contains "live trade" / "echtes Kapital" / "real money", inject confirmation banner |

---

## 10. Centralized Configuration (Single Source of Truth)

**Hard rule.** All numerical thresholds, limits, parameters, and tunables live in **one single settings module** — never duplicated, never hardcoded.

- Canonical location: `shared/config/settings.py` (Pydantic Settings) backed by environment-specific values in `.env`.
- All services and agents import from this module — no parallel constants, no scattered defaults.
- **MVP knobs that must live there:** every limit in §1 (15% concentration cap, per-cycle spending cap), `TRADING_MODE` (§4), `MAX_CAPITAL_EUR` (§3), edge threshold, cycle period, agent timeout, web-search timeout + blacklist + model, lessons-injected-per-cycle (`N`), retention windows for `market_snapshots` and `inference_log` blobs.
- **Risk-layer interaction (§3).** The hardest gates physically live inside `risk/`. The settings module imports and re-exports them — does not duplicate.
- **Validation.** Pydantic Settings + `mypy --strict`: missing or wrong-typed values fail at startup, never silently at runtime.
- **Anti-pattern enforcement.** A CI lint rejects PRs that introduce numeric literals in `execution/`, `research/`, or `risk/` outside the settings module (allowlist for trivial constants like `0`, `1`, `2`).

This is the dual of §3: §3 prevents AI from changing the *hardest* limits without humans; §10 prevents anyone (human or AI) from scattering tunables.

---

## 11. Strict Typing & Property-Based Tests

- `mypy --strict` is a hard CI gate.
- All order/position/trade/decision/prediction objects = Pydantic models. No untyped dicts on those paths.
- Risk-engine functions covered by `hypothesis` property tests, e.g. *"for any (proposed_notional, equity, open_orders), the clipped notional never exceeds 15% of equity AND never violates solvency."*
- Coverage: `risk/` 100%, `execution/` ≥ 90%, rest ≥ 80%.

---

## 12. Reference Tech Stack (MVP)

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Package manager | `uv` |
| Async runtime | `asyncio` (`uvloop` optional) |
| Storage | Postgres 16, single tier (per `data_infrastructure.md` MVP scope) |
| Object store | Filesystem under `./data/` |
| Secrets | `.env` (gitignored) |
| Orchestration | `docker-compose` for Postgres; `cron` (or `/loop`) for the cycle |
| Observability | Structured JSON logs to stdout + file |
| LLM (research / web search) | OpenAI Responses API + `web_search_preview` (model in §10) |
| LLM (decision) | Anthropic Claude Opus |
| Migrations | Alembic |
| Lint / format / types | `ruff`, `mypy --strict` |
| Tests | `pytest`, `hypothesis`, `pytest-cov` |
| Pre-commit | `gitleaks`, `trufflehog`, `ruff`, `mypy` |

---

## 13. Weiterer Ausbau (post-MVP)

Everything below is deferred until the MVP prototype runs paper-mode and shows signal. Each item is added only when a measured gap forces it. Items are grouped by which engineering-area they extend.

**Storage / data tiers** (extends §12, source of truth `data_infrastructure.md`):
- TimescaleDB hypertable for sub-second `market_snapshots` (post-MVP — when per-second cadence is needed).
- Redis for hot state + pub/sub.
- S3 / MinIO for cold-archive of `inference_log` blobs and screenshots.

**Compute / deployment** (extends §12):
- Docker image for the cycle; ECS Fargate Scheduled Tasks or k8s `CronJob` as the eventual scheduler.
- AWS as the eventual host (the user has $10k credits earmarked for it).
- Self-hosted or managed observability stack (Prometheus + Grafana + Loki + Tempo) once the operator footprint demands it; CloudWatch + Sentry as a lighter alternative.

**Secrets / signing** (extends §7):
- AWS KMS or HashiCorp Vault for API tokens.
- Cloud KMS or YubiHSM for the EIP-712 signing key — required at the moment of the `paper → real_capital` switch and not before.

**Agent-Teams production runtime** (owned by `orchestration.md §2`):
- Spawning the trading cycle as a Claude Code Agent Team (Lead + Members + Subagents) with `--dangerously-skip-permissions`, fresh-team-per-cycle.
- Production-cycle hooks (`SessionStart` janitor, `TeammateIdle`, `TaskCreated`, `TaskCompleted`, cleanup-assertion in `Stop`).
- Permission-mode boundaries for the production cycle vs. development sessions.

**Multi-team architecture** (owned by `orchestration.md §1`, `trading_feedback.md`, `optimization.md`):
- Tier-1 Trade Evaluation Team (1-min cron) and Tier-2 Code Evaluation Team (daily / weekly batches) as separate scheduled processes.
- Capital-allocation feedback service (`meta-allocator`, weekly).

**Multi-agent ensemble** (owned by `trading.md §2`):
- The 7-persona heterogeneous roster.
- Per-agent `notes` + `beliefs` + position-thesis beliefs.
- Strategy Skill Library (named-skill pattern from Voyager).

**GitHub / review automation** (extends §8):
- Claude Code GitHub App: auto PR reviews, `@claude` mentions, fix pushes.
- `/ultrareview` before every merge into `main` touching `execution/` or `risk/`.
- `/security-review` on every PR touching auth, signing, or secrets.
- GitHub Actions with `claude -p` (headless): AI-code lint, regression detection.

**Subagents (`.claude/agents/`)** — post-MVP:
- `strategy-researcher`, `risk-reviewer`, `security-reviewer`. Each with a tightly-scoped tool allow-list.

**Skills (`.claude/skills/`)** — post-MVP, on-demand:
- `polymarket-api`, `paper-mode-protocol`, `incident-response`.

**Custom slash commands (`.claude/commands/`)** — post-MVP:
- `/mode`, `/kill-all`, `/risk-rules`, `/audit <decision_id>`.

**Workflow / parallel work** — post-MVP:
- Plan-mode discipline for non-trivial changes (already used by the operator manually).
- Writer/Reviewer split via fresh sessions.
- Claude Code Desktop App for parallel worktrees.
- Claude Code on the Web for longer autonomous research on cloud VMs.

**MCP servers in dev sessions** — post-MVP:
- Postgres/TimescaleDB, Polymarket-API, Grafana/Sentry, Linear/Notion. Never wired into live trading-loop services — only developer/Claude sessions.

**Automatic safety controls** — post-MVP (intentionally manual in MVP):
- Drawdown trip-wires for the kill switch (today: monitored, operator decides).
- Circuit-breaker on consecutive cycle-failures setting the system to monitor-only mode.

**Strategy lifecycle governance** — owned by `optimization.md §4` + `trading_feedback.md §7`:
- Anti-whipsaw rule (≥ 5–7d in `real_capital` before a strategy may be replaced).
- Paper-mode promotion guidance (≥ 30d in paper before flipping to `real_capital`).

---

## See also

- `specs.md` — architecture diagram and entry point.
- `trading.md` — what the cycle does on top of these guard rails.
- `orchestration.md` — runtime topology (post-MVP elaborations heavily deferred there too).
- `data_infrastructure.md` — schemas, MVP single-tier storage, OpenAI web-search intake.
- `trading_feedback.md` — Tier 1 evaluation that writes outcomes (post-MVP team).
- `optimization.md` — Tier 2 self-improvement loop (post-MVP team).
