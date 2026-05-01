# Engineering — Risk, Safety, Repository Conventions, AI-Coding Workflow

How humans + AI build, modify, and operate the system. Owns: the deterministic risk gates (values), the safety-controls layer (kill-switch, sanity gates, audit trail), the repository conventions, CI hooks, secret management, mode flags, permissions, central settings, and the reference tech stack.

**What lives here:** values, governance, deterministic guard rails. **What does not live here:** runtime topology (`orchestration.md`), schemas + adapters + observability (`data_infrastructure.md`), agent behavior (`trading.md`).

---

## 1. Risk Management

PA-aligned: three deterministic gates plus the constitutional capital cap. All limits are plain constants in `risk/` (§9), never AI outputs.

**Per-trade gates** (applied in order; trade rejected on first failure):

1. **Concentration** — proposed notional ≤ **15% of equity** in any single market (PA standard).
2. **Solvency** — cash ≥ proposed notional + estimated fees + open-order reservations. Estimate uses Polymarket-API fee data when available, conservative fallback otherwise.
3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap (default 25% of equity, central setting §21).

**Constitutional cap (§10):**
- `MAX_CAPITAL_EUR` is a hard, two-human-approval-only ceiling on gross deployed capital. Independent of all other gates.

**Per-agent allocation:**
- Initial: equal weight (1/N).
- Rebalanced weekly by `meta-allocator` on rolling 30d hit rate + PnL (softmax, T=0.5). Full mechanics: `orchestration.md §3`.
- An agent that the operator manually flags as broken can be disabled; no automatic suspend rule.

**Manual kill-switch (§2).**
- Operator can halt all new orders via the kill-switch flag in Redis. Existing positions remain open; the safety-watchdog respects the flag at the gate. There are no automatic drawdown trip-wires — drawdown is monitored (`trading_feedback.md §5`) and surfaced via alerts (`data_infrastructure.md §3`), but action is the operator's call.

**Resolution risk (Polymarket-specific, deterministic):**
- Polymarket resolves via UMA optimistic oracle (~2–7 day delay, dispute possible). Surfaced in agent prompts as market metadata; no automatic position reduction.

---

## 2. Safety & Controls

**Kill switch:**
- Global Redis flag `system:kill_switch`.
- HTTP `POST /admin/kill` (auth: signed token + 2FA in prod).
- Order-placing services poll every 1s; on activation halt new orders. Existing positions remain open unless the operator explicitly closes them via `/manual override`.
- **Manual trigger only** (PA-aligned lean model): no automatic trip-wires. Alerts (`data_infrastructure.md §3`) page the operator on drawdown / reconciliation diff / sustained errors; the operator decides whether to flip the switch. The only fully-automatic guard is the constitutional `MAX_CAPITAL_EUR` cap (§10), enforced inside every order-submit path.

**Manual override (CLI + minimal web UI):**
- Pause/resume agent; close specific position (immediate market); edit per-agent allocation; adjust limits without restart (config in Postgres, hot-reloaded); force kill switch on/off.

**Sanity gates (block before submission):**
- Order size > 50% of equity → block.
- Order price ∉ (0.005, 0.995) → block.
- > 100 orders / hour → throttle to queue.
- > 50 simultaneous open positions → require manual approval for new.

**Audit trail:** all admin actions logged immutably (append-only table + S3 with object lock). 90d minimum retention.

**Secrets:**
- Private keys in cloud KMS or hardware key (YubiHSM solo).
- API tokens in vault (HashiCorp Vault or AWS Secrets Manager).
- No secrets in committed env files.

**Evaluation-tier safety boundary** (also see `trading_feedback.md §1` and `optimization.md §1`):
- **Trade Evaluation Team:** read-only access to Polymarket Gamma API for resolution lookup; **no signing keys, no order endpoints**. Writes only ground-truth fields on existing rows (`outcome`, `realized_pnl`, `agent_performance`).
- **Code Evaluation Team:** **no credentials** for production CLOB endpoints. Its proposals materialize as Git PRs + `proposals` rows — never direct in-process state changes. Reads all observational data; writes only `lessons`, `patterns`, `proposals`, PR commits on feature branches.
- §2 kill switch + §1 risk limits unaffected by any evaluation-team action.

Result: no runaway self-modification. Human in loop on every merge that affects live behavior.

---

## 3. Repository Layout

```
autonomous_trading/
├── research/        # strategies, agent prompts, skill library
│   ├── agents/      # per-agent persona + prompt + tools
│   └── skills/      # executable skill snippets (trading.md §8)
├── execution/       # order routing, CLOB client, position management
├── risk/            # limits, kill switches, sanity gates, capital gate
├── shared/          # data models, schemas, common utilities
│   └── config/      # central settings (§21)
├── infra/           # Docker, k8s, migrations, deployment
├── tests/           # mirrors src layout
├── docs/            # ADRs, runbooks, risk rules, code-eval decision records
├── .claude/         # project-scoped Claude Code config (committed)
├── CLAUDE.md        # AI-coding guard rails (committed)
├── CLAUDE.local.md  # personal notes (gitignored)
├── trading.md       # the trading runtime spec
├── orchestration.md # how the teams run
├── data_infrastructure.md # data + adapters + observability
├── engineering.md   # this document
├── trading_feedback.md
├── optimization.md
└── specs.md         # entry point with architecture diagram
```

`risk/` is *protected code* — see §9.

---

## 4. CLAUDE.md (project)

Short, high-signal, < 200 lines. Every line must answer *yes* to: "Would Claude make a mistake without this line?" Initial scaffold via `/init`, then manually pruned.

Mandatory:
- Build/test/lint commands (`pytest`, `ruff`, `mypy --strict`)
- No-go list:
  - **NEVER** commit real API keys, private keys, mnemonics, `.env*`
  - **NEVER** trigger live trades without explicit user confirmation in this session
  - **NEVER** modify code under `risk/` outside Plan Mode with explicit approval
  - **NEVER** push directly to `main` or `--force` push
- Repo conventions (commit format, branch naming, PR template ref)
- Pointer to `specs.md` as architectural overview, plus the four component spec files
- `@docs/risk-rules.md` import for hard risk rules

Personal/transient → `CLAUDE.local.md` (gitignored). Global → `~/.claude/CLAUDE.md`.

---

## 5. GitHub Integration

- **Claude Code GitHub App** (`/install-github-app`): auto PR reviews, `@claude` mentions, fix pushes.
- **`gh` CLI** required locally — token-cheaper for AI use.
- **`/ultrareview`** before every merge into `main` touching `execution/` or `risk/`.
- **`/security-review`** on every PR touching auth, signing, or secrets.
- **GitHub Actions** with `claude -p` (headless): AI-code lint, regression detection on tests.
- **Branch protection on `main`**: required PR reviews (≥ 2 for `risk/`), required status checks (`tests`, `security-review`, `mypy-strict`, `gitleaks`), no direct pushes, no force-push.

---

## 6. Hooks (`.claude/settings.json`)

Hooks are *deterministic* guarantees — CLAUDE.md is a request, hooks are enforcement.

| Hook | Trigger | Action |
|---|---|---|
| `PostToolUse` Edit/Write | After code edit | `ruff` + `mypy --strict` + relevant `pytest` subset; block on failure |
| `PreToolUse` Bash | Before bash | Block `rm -rf`, `git push --force`, `git reset --hard`; block writes to `migrations/`, `secrets/`, `.env*` |
| `PreToolUse` Edit/Write on `risk/**` | Before risk-code edit | Block unless session in Plan Mode with prior user approval |
| `Stop` | Before turn end | `gitleaks`/`trufflehog` on staged diff; abort on any secret hit |
| `UserPromptSubmit` | On user prompt | If contains "live trade"/"echtes Kapital"/"real money", inject confirmation banner |
| `SessionStart` (production trading-cycle only) | Lead boots | Janitor: sweep stale `~/.claude/teams/{team-name}/` directories from prior cycles that crashed without cleanup; assert team-spec source-of-truth at `.claude/teams/trading-team.spec.json` exists and parses; check Claude Code version pin; abort cycle if any check fails (next scheduled cycle retries) |
| `TeammateIdle` (production trading-cycle only) | Member goes idle | If task incomplete or no artifact written, exit 2 to keep member working; if member idles 3× in a row on the same task, abort cycle and emit alert (no kill-switch trip — a single bad cycle is non-fatal) |
| `TaskCreated` (production trading-cycle only) | Lead creates task | Validate task schema matches `trading.md §5` cycle; reject malformed tasks before members claim |
| `TaskCompleted` (production trading-cycle only) | Member marks task done | Validate output artifact against §13 Pydantic schema; exit 2 to force retry on schema mismatch; exit 0 only on clean artifact |
| `Stop` (production trading-cycle only) | Lead about to exit | Assert `clean up the team` was called; if not, force cleanup before allowing exit (Cloud-doc warning: avoid orphaned `~/.claude/teams/` entries) |

---

## 7. Subagents (`.claude/agents/`)

| Agent | Tools | Purpose |
|---|---|---|
| `strategy-researcher` | Read, Grep, WebSearch | Read-only strategy research; drafts paper-mode test plans |
| `risk-reviewer` | Read | Diff `risk/` and trading-decision paths against `docs/risk-rules.md` |
| `security-reviewer` | Read, Bash (scoped grep/gitleaks) | Secrets, injection, signing-path review |

---

## 8. Skills (`.claude/skills/`)

On-demand: `polymarket-api` (endpoints, rate limits, EIP-712 signing, fees); `paper-mode-protocol` (how to validate a strategy in paper mode, when to consider promotion to real-capital, see §11 / `trading_feedback.md §7`); `incident-response` (kill-switch, position-close, key rotation).

---

## 9. Risk Layer Protection

`risk/` contains the *deterministic* guard rails (size caps, daily loss caps, kill-switch triggers, capital gate). AI decides *what* to trade; risk layer decides *whether and how much*.

- All §1 limits = plain constants/config in `risk/`, never AI outputs.
- AI agents may not edit `risk/` outside Plan Mode + explicit user approval (§6 hook).
- All trading-decision paths import from `risk/`. Direct CLOB calls bypassing `risk/` forbidden by import-linter in CI.
- `risk/` requires 100% line coverage; CI fails below.

---

## 10. Capital Gate

Hardcoded absolute capital cap, single constant:

```python
# risk/capital_gate.py
MAX_CAPITAL_EUR: Final = <TBD_CAPITAL_CAP_EUR>
```

- `position-manager` rejects any order pushing gross deployed capital above `MAX_CAPITAL_EUR`.
- Constant changed only via: PR with required ≥ 2 reviewer approvals (branch protection), explicit rationale in PR description, audit-log entry on merge.
- Decreases also gated to ≥ 1 reviewer (prevent panic over-reduction).
- Reviewed quarterly.

---

## 11. Operational Modes (paper vs. real-capital)

The system has exactly two operational modes, controlled by a single flag in the central settings file (§21):

```
TRADING_MODE = "paper" | "real_capital"
```

- **`paper` (default).** Every step of the cycle runs identically — universe scan, agent inference, risk gates, sizing — except the `execution-engine` writes orders into a **paper-trading ledger** (Postgres table `paper_trades`) instead of routing them to Polymarket CLOB. Mark-to-market PnL uses live orderbook bids exactly like real mode. Settlement on resolution likewise. The §1 risk gates and `MAX_CAPITAL_EUR` apply to paper notional too — the simulation is intentionally faithful, including a paper "cash" balance that depletes with paper trades.
- **`real_capital`.** The `execution-engine` routes real orders to the Polymarket CLOB with EIP-712-signed transactions (`data_infrastructure.md §2`). All other behavior identical to paper mode.

**Switching modes is manual.** Flipping `TRADING_MODE` is a code change in the central settings file: PR with ≥ 1 reviewer approval (real → paper, defensive direction) or ≥ 2 reviewer approvals (paper → real_capital, offensive direction); audit-log entry on merge. **Never set via env var, never set at runtime.** A human eyeballs the diff every time the system starts trading real capital.

**Default for new branches / fresh deploys.** `paper`. A clean checkout cannot trade real capital without an explicit settings-file change.

**Backtests.** Out of scope for v1 — Polymarket markets are too thin and too short-lived for a meaningful historical backtest harness. Paper mode replaces the backtest gate (see `trading_feedback.md §7` for paper-mode promotion guidance).

---

## 12. Secret Management

- `.env*` in `.gitignore`, blocked by §6 hook on write.
- `gitleaks` + `trufflehog` in `.pre-commit-config.yaml`; same on `Stop` hook.
- Private keys never in repo/env: KMS or hardware only.
- API tokens in HashiCorp Vault or AWS Secrets Manager; fetched at startup, never persist to disk.
- Key rotation: `docs/runbooks/key-rotation.md`.

---

## 13. Strict Typing & Property-Based Tests

- `mypy --strict` is hard CI gate.
- All order/position/trade/decision/prediction/`PortfolioState` objects = Pydantic models. No untyped dicts on those paths.
- Risk-engine functions covered by `hypothesis` property tests, e.g. *"for any (proposed_notional, equity, open_orders), the clipped notional never exceeds 15% of equity AND never violates solvency"*.
- Coverage: `risk/` 100%, `execution/` ≥ 90%, rest ≥ 80%.

---

## 14. Custom Slash Commands (`.claude/commands/`)

| Command | Purpose |
|---|---|
| `/mode` | Print current `TRADING_MODE` and recent mode-switch history (read-only) |
| `/kill-all` | Trigger kill switch (with confirmation; `disable-model-invocation` set so only humans can run it) |
| `/risk-rules` | Print effective limits from `risk/` |
| `/audit <decision_id>` | Replay AI decision: prompt, model, tools, output, market data |

---

## 15. Permission Modes & Sandboxing

**Development sessions** (a human is at the terminal):
- `/permissions` allowlist for safe read-only/local commands (`pytest`, `git diff`, `ruff`, `gh pr view`).
- Auto-mode permitted only for `docs/`, `tests/`, read-only `research/` exploration.
- Auto-mode **forbidden** for any path that can reach live trading endpoints.
- `/sandbox` (OS-level) for any code with network access running unsupervised.
- For development against live-trading endpoints (e.g. running a one-off CLI), interactive permission confirmation is required every session — never allowlisted.

**Production trading-cycle session** (no human at the terminal — one fresh Claude Code Agent Team per cycle, ~12 min lifetime, per `orchestration.md §2`):
- The Lead is launched with `--dangerously-skip-permissions` so it does not block on prompts. This is unavoidable for unattended operation. Blast radius is bounded to the single cycle's lifetime — a misbehaving cycle cannot accumulate damage across cycles, and the next scheduled cycle starts clean.
- Safety in this mode comes from:
  1. **§6 hooks** — deterministic guard rails on tool use (block `rm -rf`, block writes to `risk/`, secret scanning, `SessionStart` janitor, `Stop` cleanup-assertion, etc.). Hooks fire regardless of permission mode.
  2. **§1 risk gates + §2 kill-switch** — the only authoritative gate on whether a trade is placed. The team's permission mode is irrelevant here; the risk-engine member can produce a `Decision`, but the deterministic position-manager (`risk/`) and execution-engine still enforce caps and refuse out-of-bounds orders. These services run as separate, long-lived processes — they do **not** die with the cycle.
  3. **`orchestration.md §2` team hooks** — `TeammateIdle`, `TaskCreated`, `TaskCompleted` validate every artifact before downstream members consume it.
  4. **Network egress allowlist** — the production server can only reach Polymarket CLOB, Anthropic API, configured data sources; everything else is blocked at the firewall, so even a malformed tool call cannot exfiltrate or hit unintended endpoints.
  5. **Filesystem isolation** — production server writes only to repo-local `~/.claude/`, the Postgres/Redis network sockets, and S3 (scoped IAM). No general filesystem access.
  6. **Bounded lifetime** — every cycle process exits after ~12 min by design; a process that fails to exit is killed by the scheduler (e.g. cron + timeout, k8s `activeDeadlineSeconds`).
- The trading-cycle Lead is the **only** environment where `--dangerously-skip-permissions` is acceptable. Every other Claude Code session (dev, Trade-Evaluation runs, Code-Evaluation batches, debug) keeps standard permissions.

---

## 16. Workflow Discipline

- **Plan Mode** (Shift+Tab) for any non-trivial change.
- **Subagents** for investigation; keeps main context clean.
- `/clear` between unrelated tasks.
- `/rewind` / Esc-Esc to undo, never destructive bash recovery.
- **Writer/Reviewer split**: one session writes, fresh session reviews `execution/` + `risk/` changes.
- `/statusline` configured for live token usage.

---

## 17. Parallel Work Setups

- **Claude Code Desktop App** for multiple isolated worktrees in parallel.
- **Claude Code on the Web** for longer autonomous research on cloud VMs.
- **Agent Teams** for orchestrated multi-session workflows on complex refactors.

---

## 18. Audit Trail (development reinforcement of `data_infrastructure.md §3`)

Every AI decision replayable from logs alone. Required per decision:
- Full input prompt (template + filled context)
- Model id + version + temperature/params
- Raw output (with tool calls + reasoning trace)
- Snapshot of market data used (orderbook, news bundle URI)
- All risk-gate evaluations + results
- Final action taken/rejected, with reason

Stored in `decisions` table (`data_infrastructure.md §1`); reasoning blob in S3. Replay via `/audit <decision_id>`.

---

## 19. MCP Servers

`claude mcp add` for external systems used during *development*:
- Postgres/TimescaleDB (read-only for research, read-write only for migrations)
- Polymarket API (custom MCP if no public; read-only by default)
- Grafana/Sentry (observability into Claude session)
- Linear or Notion (strategy specs, ticket sync)

MCP servers are **never** wired into live trading-loop services — only developer/Claude sessions.

---

## 20. Development-Side Observability

Reinforces `data_infrastructure.md §3`:
- Sentry for exceptions in all services.
- Grafana dashboard URL in PR template.
- Alerts on anomalous trade volume routed to operator phone (never to Claude/AI).

---

## 21. Centralized Configuration (Single Source of Truth)

**Hard rule.** All numerical thresholds, limits, parameters, and tunables that influence runtime behavior live in **one single settings file** — never duplicated, never hardcoded as magic numbers in service code. The user must be able to change every operational knob from one file.

- Canonical location: `shared/config/settings.py` (Pydantic Settings model) backed by `config/settings.toml` for the values themselves. Single source of truth.
- All services and agents import from this module — no parallel constants, no scattered defaults, no magic numbers in business logic.
- **Examples of values that MUST live there** (non-exhaustive): every limit in §1 (15% concentration cap, per-cycle spending cap), `TRADING_MODE` flag (§11), edge threshold (`trading.md §6`), cycle period and per-stage timeouts (`trading.md §5`), agent timeout, reconciler interval / diff threshold (`data_infrastructure.md §2`), retry/backoff parameters (`data_infrastructure.md §2`), fee estimator constants, top-K for belief injection (`trading.md §2`), notes cap, position-thesis adverse-move threshold (`trading.md §2`), Code-Eval explore/exploit budget ratios + drawdown-tilt and outperformance-tilt thresholds (`optimization.md §3`), weekly-batch top-K size (`optimization.md §3`), strategy-displacement minimum days (`trading.md §3.1`).
- **Risk-layer interaction (§9, §10).** The hardest gates (`MAX_CAPITAL_EUR`, kill-switch triggers, all `risk/`-owned limits) physically live inside `risk/` for protection. The settings file imports and re-exports them — it does **not duplicate** them. Users still see one file with all knobs; `risk/` retains its 2-human-review enforcement on the source.
- **Validation.** Pydantic Settings + `mypy --strict`: missing or wrong-typed values fail at service startup, never silently at runtime.
- **Change governance.** Edits to non-`risk/` settings require ≥ 1 reviewer + CI tests. Edits to `risk/`-owned values keep the §9/§10 stricter rules (≥ 2 reviewers, audit-log entry). `TRADING_MODE` paper → real_capital follows §11 review counts.
- **Hot-reload.** Non-critical operational knobs (scan filters, dashboard intervals) hot-reload from Postgres-backed config (§2) without restart. Structural values (cycle period, schema fields, `TRADING_MODE`) are restart-only.
- **Anti-pattern enforcement.** A CI lint rule rejects PRs that introduce a numeric literal in `execution/`, `research/`, or `risk/` outside the settings module (allowlist for trivial constants like `0`, `1`, `2`). Forces every new tunable through the settings file.

This rule is the dual of §9 risk-layer protection: §9 prevents the AI from changing the *hardest* limits without humans; §21 prevents anyone (human or AI) from scattering tunables so widely that no single file shows the full operating envelope.

---

## Appendix A — Reference Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 (services), Rust (hot-path execution) |
| API framework | FastAPI |
| Async runtime | asyncio + uvloop; Tokio for Rust |
| Message bus | Redis Streams (v1) → NATS JetStream (v2) |
| Relational DB | Postgres 16 |
| Time-series | TimescaleDB extension |
| Cache | Redis 7 |
| Object store | MinIO local, S3 prod |
| Orchestration | Docker Compose (v1) → Kubernetes (v2) |
| Observability | Prometheus + Grafana + Loki + Tempo |
| LLM provider | Anthropic — **Claude Opus only** (single-vendor, single-model-family by design) |
| Vector store | pgvector (research bundle dedup) |
| Secrets | HashiCorp Vault |
| Signing | EIP-712 via web3.py / ethers-rs |

---

## See also

- `orchestration.md` — runtime topology, team mechanics, capital allocation.
- `data_infrastructure.md` — schemas, prediction-market interface, observability.
- `trading.md` — what the trading runtime does on top of these guard rails.
- `trading_feedback.md` — Tier 1 evaluation, paper-mode promotion guidance.
- `optimization.md` — Tier 2 code evaluation, governance for code changes (`§7` review counts).
- `specs.md` — architecture diagram and entry point.
