# Engineering — Risk, Safety, Repository Conventions, AI-Coding Workflow

How humans + AI build, modify, and operate the system. Owns the deterministic risk gates, kill-switch, audit-trail wiring, repository conventions, secret handling, mode-flag governance, central settings module, CI/branch-protection, and the reference tech stack.

**What lives here:** values, governance, deterministic guard rails, and the engineering scaffolding to build the repo. **What does not live here:** what the bot does (`trading.md`, `specs.md`), runtime topology (`orchestration.md`), schemas + adapters + sources (`data_infrastructure.md`), Tier-1 evaluation behavior (`trading_feedback.md`), Tier-2 evaluation behavior (`optimization.md`).

---

## MVP scope (engineering only)

Build a minimum repo that runs the prototype described in `specs.md` + `trading.md`. The engineering decisions for that build are:

- **Single language:** Python 3.12, `uv`-managed.
- **Single storage tier:** Postgres 16 only (per `data_infrastructure.md` MVP scope). No Redis, no TimescaleDB, no S3, no Vault, no AWS Secrets Manager.
- **Single deployment target:** local. `docker-compose` for Postgres; the trading cycle is a fresh Claude Code Agent Team per `cron` tick (per `trading.md §2`). No k8s, no ECS.
- **Real_capital from day 1.** EIP-712 signing key is in scope (§7). AWS KMS is the recommended `KeyProvider` backend; encrypted-at-rest local file is the acceptable fallback. Plaintext keys are forbidden.
- **Strict CI gates from day 1:** `mypy --strict`, `ruff`, `pytest`, `gitleaks`. Risk-layer 100% coverage hard gate.
- **Risk + safety enforcement is non-negotiable** even in MVP — the §1 gates, the §3 capital cap, the §4 mode flag, and the §9 hooks are all in scope from commit one.
- **Everything that does not directly serve the prototype is deferred** — listed in §13 *Weiterer Ausbau (post-MVP)*.

---

## 1. Risk Management

Three deterministic gates plus the constitutional capital cap. All limits are plain constants in `src/risk/`, never AI outputs.

**Per-trade gates** (applied in order; trade rejected on first failure):
1. **Concentration** — proposed notional ≤ **15% of equity** in any single market.
2. **Solvency** — available cash (paper-cash in `paper` mode, USDC in `real_capital` mode) ≥ proposed notional + estimated fees + open-order reservations.
3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap (default 25% of equity, central setting §10).

**Constitutional cap (§3).** `MAX_CAPITAL_EUR` is a hard ceiling on gross deployed capital, gated by the audit-log self-review pattern (§3, §8). Independent of all other gates. In MVP this is the paper-cash budget; on the `paper → real_capital` switch it becomes the real-USDC ceiling.

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

`src/risk/` owns the deterministic guard rails. The AI decides *what* to trade; the risk layer decides *whether and how much*.

- All §1 limits = plain constants/Settings imports in `src/risk/`, never AI outputs.
- AI agents may not edit `src/risk/` outside Plan Mode + explicit user approval (§9 hook).
- All trading-decision paths import from `src/risk/`. Direct order-adapter calls bypassing `src/risk/` are forbidden by `import-linter` in CI.
- `src/risk/` requires 100% line coverage; CI fails below.

**Capital gate.** Single constant:

```python
# src/risk/capital_gate.py
MAX_CAPITAL_EUR: Final = <TBD_BY_OPERATOR>
```

- The order-submission path rejects any order pushing gross deployed capital above `MAX_CAPITAL_EUR`.
- Constant changed only via PR. **Single-operator gate:** every change — increase or decrease — requires an `AUDIT_LOG.md` entry documenting what changed, what could go wrong, and why it's still safe. CI cannot enforce the audit entry; operator discipline does. (If a second human ever joins, raise branch-protection `required_approving_review_count` back to ≥1 and require their approval for increases.)

---

## 4. Operational Modes (paper vs. real_capital) — governance

The flag itself lives in the central settings module (§10):

```
TRADING_MODE = "paper" | "real_capital"
```

Engineering owns the **governance** of this flag — what the runtime does with it is in `trading.md` and `data_infrastructure.md §2`.

- **Default `paper`.** A clean checkout cannot trade real capital without an explicit settings-file change.
- **Switching is manual.** PR with an `AUDIT_LOG.md` entry on merge. The entry must document the operator's safety review — what's changing, what could go wrong, why it's still safe. **Never via env var, never at runtime.** (Single-operator project — see §3, §8; if a second human ever joins, additionally require their approval for `paper → real_capital`.)
- **No backtest harness.** Polymarket markets are too short-lived; paper-mode is the validation gate (`trading_feedback.md §4` owns the promotion criteria).

---

## 5. Repository Layout

```
autonomous_trading/
├── src/research/        # agent prompt + skills (web_search, etc.)
├── src/execution/       # paper-trade ledger; later, CLOB client
├── src/risk/            # limits, kill switch, sanity gates, capital_gate (§3)
├── src/shared/
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

`src/risk/` is *protected code* — see §3.

---

## 6. CLAUDE.md (project)

Short, high-signal, < 200 lines. Every line must answer *yes* to: "Would Claude make a mistake without this line?"

Mandatory:
- Build/test/lint commands (`pytest`, `ruff`, `mypy --strict`, `alembic upgrade head`).
- No-go list:
  - **NEVER** commit real API keys, mnemonics, `.env*`.
  - **NEVER** trigger live trades without explicit user confirmation in this session.
  - **NEVER** modify code under `src/risk/` outside Plan Mode with explicit approval.
  - **NEVER** push directly to `main` or force-push.
- Pointer to `specs.md` and the 6 component spec files.

Personal/transient → `CLAUDE.local.md` (gitignored).

---

## 7. Secret Management (MVP)

- `.env*` in `.gitignore`. `gitleaks` + `trufflehog` in `.pre-commit-config.yaml` and on every PR.
- API tokens (Anthropic, OpenAI, Polymarket-read) live in `.env` for MVP — local `.env` is sufficient.
- **EIP-712 signing key is in scope from MVP day 1**, because MVP supports `real_capital` (`engineering.md §4`). Pragmatic options for the MVP:
  - **Preferred:** AWS KMS (or equivalent cloud KMS) accessed via `KeyProvider` adapter — no plaintext key on disk; `boto3.client("kms").sign(...)` for the signing call.
  - **Acceptable:** encrypted-at-rest local key file with passphrase prompt at process start, behind the same `KeyProvider` interface; explicitly inferior to KMS, used only if cloud-KMS setup is blocking.
  - **Forbidden:** plaintext key in `.env` or any committed file. `gitleaks` + `trufflehog` block this.
- The `KeyProvider` interface is the boundary; swapping local-encrypted for cloud-KMS later is a config change, not a code change.

---

## 8. GitHub Integration (MVP)

- **`gh` CLI** required locally — token-cheaper for AI use.
- **Branch protection on `main`:**
  - `required_approving_review_count: 0` — single-operator project; GitHub forbids self-approval, so the platform-enforced approval count must be 0 for any PR to merge. Risk-sensitivity is enforced via the audit-log self-review pattern instead (see below).
  - Required status checks: `lint` (ruff), `type-check` (mypy --strict), `gitleaks`, `trufflehog` from Phase 1; `pytest` + `import-linter` + `src/risk/`-100%-coverage from Phase 3.
  - `enforce_admins: true`, no direct pushes, no force-push, no deletions.
- **Single-operator audit pattern.** The operator is both author and reviewer. For any PR touching `src/risk/**`, `MAX_CAPITAL_EUR`, or flipping `TRADING_MODE`, an `AUDIT_LOG.md` entry is mandatory and must document: what changed, what could go wrong, why it's still safe. CI cannot enforce the audit entry; the operator's discipline does. The audit log is the second-review trail. If a second human operator ever joins, raise `required_approving_review_count` to ≥1 and require their approval on the same set of paths.

---

## 9. Hooks (`.claude/settings.json`) — MVP set

Hooks are deterministic guarantees. CLAUDE.md is a request, hooks are enforcement. MVP runs **two distinct hook contexts** because the trading cycle is itself a Claude Code Agent Team (`trading.md §2`):

**Development-session hooks** (a human is at the terminal):

| Hook | Trigger | Action |
|---|---|---|
| `PostToolUse` Edit/Write | After code edit | `ruff` + `mypy --strict` + relevant `pytest` subset; block on failure |
| `PreToolUse` Bash | Before bash | Block `rm -rf`, `git push --force`, `git reset --hard`; block writes to `.env*` |
| `PreToolUse` Edit/Write on `src/risk/**` | Before risk-code edit | Block unless session in Plan Mode with prior user approval |
| `Stop` | Before turn end | `gitleaks` on staged diff; abort on any secret hit |
| `UserPromptSubmit` | On user prompt | If contains "live trade" / "echtes Kapital" / "real money", inject confirmation banner |

**Production trading-cycle hooks** (Lead boots fresh team per cycle, `--dangerously-skip-permissions`, no human at the terminal):

| Hook | Trigger | Action |
|---|---|---|
| `SessionStart` | Lead boots | Janitor sweeps stale `~/.claude/teams/{team-name}/` directories from prior crashed cycles; assert team-spec source-of-truth (`.claude/teams/trading-team.spec.json`) exists and parses; abort cycle if any check fails (next scheduled cycle retries) |
| `TaskCreated` | Lead creates task for member | Schema-validate task payload against the §11 Pydantic models; reject malformed tasks before claim |
| `TaskCompleted` | Member marks task done | Pydantic-validate the output artifact (`Universe`, `PortfolioState`, `Prediction[]`, `Decision[]`); exit 2 to force retry on schema mismatch; exit 0 only on clean artifact |
| `Stop` | Lead about to exit | Assert `clean up the team` was called; if not, force cleanup before allowing process exit |

The dev-session hooks fire regardless of permission mode — they remain active in production-cycle context too. The production-cycle-specific hooks only meaningfully fire in the Lead's session.

---

## 10. Centralized Configuration (Single Source of Truth)

**Hard rule.** All numerical thresholds, limits, parameters, and tunables live in **one single settings module** — never duplicated, never hardcoded.

- Canonical location: `src/shared/config/settings.py` (Pydantic Settings) backed by environment-specific values in `.env`.
- All services and agents import from this module — no parallel constants, no scattered defaults.
- **MVP knobs that must live there:** every limit in §1 (15% concentration cap, per-cycle spending cap), `TRADING_MODE` (§4), `MAX_CAPITAL_EUR` (§3), edge threshold, cycle period, agent timeout, web-search timeout + blacklist + model, lessons-injected-per-cycle (`N`), retention windows for `market_snapshots` and `inference_log` blobs.
- **Risk-layer interaction (§3).** The hardest gates physically live inside `src/risk/`. The settings module imports and re-exports them — does not duplicate.
- **Validation.** Pydantic Settings + `mypy --strict`: missing or wrong-typed values fail at startup, never silently at runtime.
- **Anti-pattern enforcement.** A CI lint rejects PRs that introduce numeric literals in `src/execution/`, `src/research/`, or `src/risk/` outside the settings module (allowlist for trivial constants like `0`, `1`, `2`).

This is the dual of §3: §3 prevents AI from changing the *hardest* limits without humans; §10 prevents anyone (human or AI) from scattering tunables.

---

## 11. Strict Typing & Property-Based Tests

- `mypy --strict` is a hard CI gate.
- All order/position/trade/decision/prediction objects = Pydantic models. No untyped dicts on those paths.
- Risk-engine functions covered by `hypothesis` property tests, e.g. *"for any (proposed_notional, equity, open_orders), the clipped notional never exceeds 15% of equity AND never violates solvency."*
- Coverage: `src/risk/` 100%, `src/execution/` ≥ 90%, rest ≥ 80%.

---

## 12. Reference Tech Stack (MVP)

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Package manager | `uv` |
| Async runtime | `asyncio` (`uvloop` optional) |
| Storage | Postgres 16, single tier (per `data_infrastructure.md` MVP scope) |
| Object store | Filesystem under `./data/` |
| Secrets (API tokens) | `.env` (gitignored) |
| Signing key | AWS KMS via `KeyProvider` adapter (preferred) OR encrypted-at-rest local file (acceptable fallback) |
| Cycle runtime | Claude Code Agent Team — fresh `claude` process per cycle (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, `--dangerously-skip-permissions`). Pinned Claude Code version in `infra/`. |
| Scheduler | `cron` triggering the per-cycle `claude` process |
| Local infra | `docker-compose` for Postgres |
| Observability | Structured JSON logs to stdout + file |
| LLM (research / web search) | OpenAI Responses API + `web_search_preview` (model in §10) |
| LLM (decision) | Anthropic Claude Opus |
| Polymarket client | `Polymarket/py-clob-client` (per `data_infrastructure.md §0`) |
| EIP-712 signing | `web3.py` (eth_account) wrapped behind `KeyProvider` adapter |
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
- AWS KMS for the EIP-712 signing key is **MVP-recommended** (not deferred); the local-encrypted-key fallback is the post-MVP-acceptable path that can be replaced by KMS once the cloud-KMS setup lands.
- AWS Secrets Manager / HashiCorp Vault for API tokens (today: `.env` is fine because it's local-only).
- YubiHSM / hardware-wallet for the signing key (post-MVP — when scaling capital justifies the operational cost).

**Agent-Teams production runtime** (now MVP per `trading.md §2`):
- The trading-cycle Lead is launched fresh per cycle with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `--dangerously-skip-permissions` from MVP day 1.
- Production-cycle hooks `SessionStart` (janitor), `TaskCreated`, `TaskCompleted`, `Stop` (cleanup-assertion) are part of the §9 MVP set.
- Permission-mode boundaries (dev session vs. production cycle) are MVP, not deferred. Safety in the production cycle comes from §1 risk gates + §2 kill switch + §3 capital gate + §9 hooks — *not* from interactive permission prompts.
- Post-MVP additions on this surface: `TeammateIdle` hook (3-strikes-and-abort), per-member subagent fan-out, multi-team coordination (Trade-Eval-Team, Code-Eval-Team).

**Multi-team architecture** (owned by `orchestration.md §1`, `trading_feedback.md`, `optimization.md`):
- Tier-1 Trade Evaluation Team (1-min cron) and Tier-2 Code Evaluation Team (daily / weekly batches) as separate scheduled processes. (MVP runs Tier-1 as a deterministic Python script per `trading_feedback.md`; Tier-2 as the manual-operator-review loop per `optimization.md §1`.)
- Capital-allocation feedback service (`meta-allocator`, weekly).

**Multi-agent ensemble** (owned by `trading.md §8 *Weiterer Ausbau*`):
- The 7-persona heterogeneous roster.
- Per-agent `notes` partitioning + `beliefs` + position-thesis beliefs + `operating_doctrine`.
- Strategy Skill Library (named-skill pattern from Voyager).

**GitHub / review automation** (extends §8):
- Claude Code GitHub App: auto PR reviews, `@claude` mentions, fix pushes.
- `/ultrareview` before every merge into `main` touching `src/execution/` or `src/risk/`.
- `/security-review` on every PR touching auth, signing, or secrets.
- GitHub Actions with `claude -p` (headless): AI-code lint, regression detection.

**Subagents (`.claude/agents/`)** — partially MVP, partially post-MVP:
- **MVP:** the 2 LLM trading-team members defined in `trading.md §2` (`trading-agent`, `risk-execution`) live here as agent definitions. The team-spec at `.claude/teams/trading-team.spec.json` references them. The universe filter is Lead-internal Python (`_python_scanner`), not an LLM agent.
- **Post-MVP review subagents:** `strategy-researcher`, `risk-reviewer`, `security-reviewer`. Each with a tightly-scoped tool allow-list. These are PR-review subagents, not trading-cycle members.

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

**Strategy lifecycle governance** — owned by `optimization.md §5` + `trading_feedback.md §4`:
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
