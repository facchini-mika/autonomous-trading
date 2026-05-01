# Engineering — Risk, Safety, Repository Conventions, AI-Coding Workflow

How humans + AI build, modify, and operate the **Self-Improving Agentic Trading Bot**. Owns the deterministic risk gates, kill-switch, audit trail, repository conventions, secrets, mode flags, central settings, and the reference tech stack — all scaled to an MVP whose **only strategy is uncovering mispricings via OpenAI web research**.

**What lives here:** values, governance, deterministic guard rails. **What does not live here:** runtime topology (`orchestration.md`), schemas + adapters (`data_infrastructure.md`), agent behavior (`trading.md`).

---

## MVP scope (this version of the doc)

The bot is **one self-improving agent** that, every cycle:
1. Picks a Polymarket market.
2. Does OpenAI web-research (`research/skills/web_search.py`) on the market's question.
3. Forms a probability estimate `p_agent`.
4. Compares to the market price `q_market`; if `|p_agent − q_market|` ≥ edge threshold, places a paper-trade.
5. After resolution: records outcome, appends a `lesson` to memory; the lesson feeds the next cycle's prompt.

Mispricing detection is the entire strategy. Research is the entire information intake. There is no other agent, no ensemble, no multi-strategy layer.

**Aggressively deferred (post-MVP), even if mentioned in the other specs:** multi-agent ensembles, the full three-team architecture, Agent-Teams production runtime, Cloud KMS / Vault / AWS Secrets Manager, hardware wallets, k8s, weekly meta-allocator, MCP servers in dev sessions, parallel-worktree workflows, smart order routing, automatic drawdown trip-wires.

Anything below that contradicts this scope is post-MVP.

---

## 1. Risk Management

Three deterministic gates plus the constitutional capital cap. All limits are plain constants in `risk/`, never AI outputs.

**Per-trade gates** (applied in order; trade rejected on first failure):
1. **Concentration** — proposed notional ≤ **15% of equity** in any single market.
2. **Solvency** — paper-cash ≥ proposed notional + estimated fees + open-order reservations.
3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap (default 25% of equity, central setting §10).

**Constitutional cap (§3).** `MAX_CAPITAL_EUR` is a hard, two-human-approval-only ceiling on gross deployed capital. Independent of all other gates. In MVP this is the paper-cash budget; on the `paper → real_capital` switch it becomes the real-USDC ceiling.

**Manual kill-switch (§2).** Operator halts all new orders by flipping a row in `system_state` (`data_infrastructure.md §1`). No automatic drawdown trip-wires in MVP — drawdown is monitored, the operator decides whether to flip.

**Resolution risk.** Polymarket UMA oracle (~2–7d delay, dispute possible). Surfaced in agent prompts as market metadata; no automatic position reduction.

---

## 2. Safety & Controls

**Kill switch:**
- A row in `system_state(key='kill_switch', value=true)` (Postgres).
- The execution-engine reads it before every order; `true` halts new orders. Existing positions stay open.
- **Manual trigger only** — no automatic trip-wires in MVP. The constitutional `MAX_CAPITAL_EUR` (§3) is the only fully-automatic guard.

**Manual override (CLI):** pause/resume the agent, close a paper-position, edit allocation, flip the kill switch.

**Sanity gates** (block before submission, in addition to §1):
- Order size > 50% of equity → block.
- Order price ∉ (0.005, 0.995) → block.
- > 100 orders / hour → throttle.
- > 50 simultaneous open positions → require manual approval.

**Audit trail.** Every cycle is replayable from Postgres alone:
- `decisions` row records the gate evaluations and the chosen action.
- `predictions.inference_log` JSONB carries the full prompt, Claude output, tool calls, and `web_search` results.
- Append-only by convention; in `real_capital` mode, DB role permissions forbid deletes.

**Self-improvement safety boundary.**
- The trading agent writes `predictions`, `decisions`, `paper_trades` (and reads everything).
- Resolution-time evaluation writes only ground-truth fields on existing rows (`outcome`, `realized_pnl`).
- Any code, prompt, or limit change proposed by the system materializes as a Git PR for human review — never an in-process state change. §1 limits + the §2 kill switch are unaffected by any agent action.

---

## 3. Risk Layer Protection + Capital Gate

`risk/` owns the *deterministic* guard rails. The AI decides *what* to trade; the risk layer decides *whether and how much*.

- All §1 limits = plain constants/Settings imports in `risk/`, never AI outputs.
- AI agents may not edit `risk/` outside Plan Mode + explicit user approval (§9 hook).
- All trading-decision paths import from `risk/`. Direct CLOB calls bypassing `risk/` are forbidden by `import-linter` in CI.
- `risk/` requires 100% line coverage; CI fails below.

**Capital gate.** Single constant:

```python
# risk/capital_gate.py
MAX_CAPITAL_EUR: Final = <TBD_BY_OPERATOR>
```

- The order-submission path rejects any order pushing gross deployed capital above `MAX_CAPITAL_EUR`.
- Constant changed only via PR; in `real_capital` mode this requires ≥ 2 reviewer approvals + an audit-log entry. Decreases also gated to ≥ 1 reviewer to prevent panic over-reduction. Reviewed quarterly.

---

## 4. Operational Modes (paper vs. real_capital)

Single flag in the central settings file (§10):

```
TRADING_MODE = "paper" | "real_capital"
```

- **`paper` (default).** Identical pipeline to `real_capital` except the `execution-engine` writes to the `paper_trades` table instead of Polymarket CLOB. Mark-to-market PnL uses live Polymarket bids; settlement on resolution likewise. §1 risk gates and `MAX_CAPITAL_EUR` apply to paper notional too.
- **`real_capital`.** EIP-712-signed orders to the Polymarket CLOB (`data_infrastructure.md §2`). Identical pipeline otherwise.

**Switching modes is manual.** PR with ≥ 1 reviewer approval (real → paper, defensive) or ≥ 2 reviewer approvals (paper → real_capital, offensive); audit-log entry on merge. **Never via env var, never at runtime.** **Default for a fresh checkout is `paper`** — a clean clone cannot trade real capital without an explicit settings-file change.

**Backtests.** Out of scope — Polymarket markets are too short-lived for a meaningful historical harness. Paper-mode is the validation gate.

---

## 5. Self-Improvement Loop (MVP form)

What makes the bot "self-improving" in MVP — and what does not:

1. **Outcome ingestion.** When a market resolves, a Trade-Evaluation step writes `outcome` and `realized_pnl` on the corresponding `predictions` row.
2. **Lesson extraction.** A daily batch reads recently resolved predictions and emits structured `lessons` rows: observation, hypothesis, action_taken, outcome, status (per `optimization.md §2`).
3. **Prompt injection.** The next cycle's agent prompt includes the most recent `N` lessons (configurable via §10 settings), so the agent's reasoning is informed by what worked and what missed.
4. **Code / prompt / limit changes are never auto-applied.** They materialize as Git PRs the operator reviews and merges. The §1 risk limits and the §2 kill switch are immune to any agent action.

That is the entire feedback loop. Multi-agent ensembles, agent rosters, capital reallocation, and prompt-mutation are post-MVP.

---

## 6. Repository Layout

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

## 7. CLAUDE.md (project)

Short, high-signal, < 200 lines. Every line must answer *yes* to: "Would Claude make a mistake without this line?"

Mandatory:
- Build/test/lint commands (`pytest`, `ruff`, `mypy --strict`).
- No-go list:
  - **NEVER** commit real API keys, private keys, mnemonics, `.env*`.
  - **NEVER** trigger live trades without explicit user confirmation in this session.
  - **NEVER** modify code under `risk/` outside Plan Mode with explicit approval.
  - **NEVER** push directly to `main` or force-push.
- Pointer to `specs.md` and the four component spec files.

Personal/transient → `CLAUDE.local.md` (gitignored). Global → `~/.claude/CLAUDE.md`.

---

## 8. Secret Management (MVP)

- `.env*` in `.gitignore`. `gitleaks` + `trufflehog` in `.pre-commit-config.yaml` and on every PR.
- API tokens (Anthropic, OpenAI, Polymarket-read) live in `.env` for MVP — local `.env` is enough because we never trade real capital here.
- **No private keys in MVP.** EIP-712 signing arrives only on the `real_capital` switch; cloud KMS or hardware key (YubiHSM) ship with that change, not before.

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

Production-cycle Agent-Teams hooks (`SessionStart` janitor, `TeammateIdle`, `TaskCreated`, `TaskCompleted`, cleanup-assertion in `Stop`) are post-MVP — they only matter once the cycle runs as an unattended Claude Code Agent Team.

---

## 10. Centralized Configuration (Single Source of Truth)

**Hard rule.** All numerical thresholds, limits, parameters, and tunables live in **one single settings module** — never duplicated, never hardcoded as magic numbers in service code.

- Canonical location: `shared/config/settings.py` (Pydantic Settings) backed by environment-specific values in `.env`.
- All services and agents import from this module — no parallel constants, no scattered defaults.
- **MVP knobs that must live there:** every limit in §1 (15% concentration cap, per-cycle spending cap), `TRADING_MODE` (§4), `MAX_CAPITAL_EUR` (§3), edge threshold, cycle period, agent timeout, web-search timeout + blacklist, lessons-injected-per-cycle (`N`), retention policies for `market_snapshots` and `inference_log` blobs.
- **Risk-layer interaction (§3).** The hardest gates physically live inside `risk/`. The settings module imports and re-exports them — does not duplicate.
- **Validation.** Pydantic Settings + `mypy --strict`: missing or wrong-typed values fail at startup, never silently at runtime.
- **Anti-pattern enforcement.** A CI lint rejects PRs that introduce numeric literals in `execution/`, `research/`, or `risk/` outside the settings module (allowlist for trivial constants like `0`, `1`, `2`).

This is the dual of §3: §3 prevents AI from changing the *hardest* limits without humans; §10 prevents anyone (human or AI) from scattering tunables.

---

## 11. Strict Typing & Property-Based Tests

- `mypy --strict` is a hard CI gate.
- All order/position/trade/decision/prediction/`PortfolioState` objects = Pydantic models. No untyped dicts on those paths.
- Risk-engine functions covered by `hypothesis` property tests, e.g. *"for any (proposed_notional, equity, open_orders), the clipped notional never exceeds 15% of equity AND never violates solvency."*
- Coverage: `risk/` 100%, `execution/` ≥ 90%, rest ≥ 80%.

---

## 12. Reference Tech Stack (MVP)

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Async runtime | `asyncio` (`uvloop` optional) |
| Storage | Postgres 16 (single tier — see `data_infrastructure.md` MVP scope) |
| Object store | Filesystem under `./data/` (deferred S3) |
| Secrets | `.env` (deferred Vault / Secrets Manager) |
| Orchestration | `docker-compose` for local infra; `cron` for the cycle (deferred k8s / ECS / Agent-Teams runtime) |
| Observability | Structured JSON logs to stdout + file (deferred Prometheus / Loki / Tempo / CloudWatch) |
| LLM (research / web search) | OpenAI Responses API + `web_search_preview` (model in §10) |
| LLM (decision) | Anthropic Claude Opus |
| Vector store | None in MVP |
| Signing | None in MVP (paper-mode only) |

Post-MVP layers (FastAPI HTTP surface, Redis, TimescaleDB, MinIO/S3, Vault, k8s, MCP servers in dev) are listed in the older revisions of this doc and re-introduced as measured needs arise.

---

## See also

- `orchestration.md` — runtime topology (apply MVP scope there too).
- `data_infrastructure.md` — schemas, MVP single-tier storage, OpenAI web-search intake.
- `trading.md` — what the cycle does on top of these guard rails.
- `trading_feedback.md` — Tier 1 evaluation that writes outcomes.
- `optimization.md` — Tier 2 self-improvement loop that emits lessons + PRs.
- `specs.md` — architecture diagram and entry point.
