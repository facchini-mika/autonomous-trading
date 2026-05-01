# Orchestration — Runtime Topology

How the MVP system runs at runtime — what processes exist, on what schedule, how they interact, and which one is actually a Claude Code Agent Team. Owns: the per-process inventory, the Trading Team's lifecycle and bootstrap, the memory split between in-process short-term state and Postgres long-term state, and the failure-handling shape that follows from "fresh team per cycle."

**What lives here:** the runtime topology and process inventory. **What does not live here:** what the Trading Team's members do (`trading.md`), schemas (`data_infrastructure.md §1`), risk limits + hooks + central settings (`engineering.md`), Tier-1 outcome math (`trading_feedback.md`), Tier-2 self-improvement loop (`optimization.md`).

---

## MVP scope (this version of the doc)

- **Exactly one Claude Code Agent Team** in MVP — the Trading Cycle (Lead + 3 members per `trading.md §2`). Everything else that used to be a "team" in the full architecture is either a deterministic Python script (Tier-1 / Tier-2) or deferred entirely.
- **Two deterministic Python scripts**: outcome ingestion (`trading_feedback.md §1`) and the daily lessons summary (`optimization.md §1`). Triggered by cron, no LLM, no Agent-Team overhead.
- **No three-team architecture in MVP** — no Trade Evaluation Team as a Claude Code Agent Team, no Code Evaluation Team, no `meta-allocator`, no 7-persona ensemble, no subagent fan-out per member.
- **Three Postgres roles** for the three runtime processes (cycle / outcome-ingestion / lessons-summary) — the load-bearing authority-boundary mechanism in MVP because real_capital is in scope from day 1.
- **Memory split** is two layers (short-term in-process, long-term Postgres) — the 10-layer taxonomy is post-MVP.
- **Everything else is post-MVP** — see §7 *Weiterer Ausbau*. Consistent with `engineering.md §13`.

---

## 1. Runtime Processes (MVP)

Three independent processes. Decoupled scheduling — a stuck process cannot block the others. They share state only through Postgres.

| Process | Schedule | Type | Purpose | Spec owner |
|---|---|---|---|---|
| **Trading Cycle** | every ~12 min (cron) | Claude Code Agent Team (Lead + 3 members) | Decide what to trade, place orders | `trading.md` |
| **Outcome Ingestion** | every ~5–10 min (cron) | Deterministic Python script (no LLM) | Write `outcome` + `realized_pnl` on resolved Polymarket markets | `trading_feedback.md §1` |
| **Lessons Summary** | daily (cron) | Deterministic Python script (no LLM) | Emit `lessons` rows from recently-resolved predictions via surprise heuristic | `optimization.md §1` |

A failed run on any process is a non-event — no orders placed, no state mutated; the next scheduled tick runs cleanly. There is no supervisor with liveness probes; cron is the supervisor.

**Postgres role separation** (load-bearing because real_capital is live):

| Role | Used by | Privileges |
|---|---|---|
| `trading_cycle` | Trading Cycle process | `SELECT` everywhere; `INSERT`/`UPDATE` on `predictions`, `decisions`, `trades`, `paper_trades`, `positions`, `cycle_plan`, `notes`, `system_state` |
| `outcome_ingestion` | Outcome Ingestion script | `SELECT` everywhere; `UPDATE` only on `predictions.outcome`/`realized_pnl`, `trades.realized_pnl`/`status`, `paper_trades.realized_pnl`/`status`, `positions.status`/`realized_pnl`/`last_updated`, `system_state.last_outcome_ingestion_at` |
| `lessons_summary` | Lessons Summary script | `SELECT` on trading tables; `INSERT` only on `lessons` |

`INSERT`/`DELETE`/`UPDATE` outside each role's grant set is rejected by Postgres at the role level. This is the mechanical enforcement of the authority boundaries declared in `trading_feedback.md §2` and `optimization.md §2`.

---

## 2. Trading Team (the only Agent Team in MVP)

The Trading Cycle = **one fresh Claude Code Agent Team per cycle**. Every cron tick starts a new `claude` process; the Lead boots the team, runs one cycle (per `trading.md §5`), calls `clean up the team`, and exits. The next scheduled tick starts a brand-new process with no shared in-process state.

**Topology** (full member detail in `trading.md §2`):
- **Lead** — drives cycle clock, spawns members, persists artifacts, writes `cycle_plan`, calls `clean up the team`. Never executes orders.
- **`scanner-reviewer`** — fetches top-K liquid Polymarket markets + builds `PortfolioState`.
- **`trading-agent`** — mispricing analysis with `web_search`; outputs `Prediction[]`.
- **`risk-execution`** — applies `risk/` gates, sizes, places paper or signed CLOB order.

Member definitions live in `.claude/agents/{scanner-reviewer,trading-agent,risk-execution}.md`. Team spec source-of-truth: `.claude/teams/trading-team.spec.json`.

**Why fresh-team-per-cycle.** The design rule *"memory is the only coupling between cycles"* is enforced by construction — a bad cycle cannot poison the next, memory leaks are physically impossible, Claude Code version upgrades pick up at the next cycle naturally. Cloud-doc constraints (no session resumption, fixed Lead, one team per session) all become non-issues because every cycle starts a fresh session anyway. Cost: ~5–30s boot per cycle, < 5% of the 12-min period and entirely before edge-time-sensitive work.

**Bootstrap (single command per cycle, run by cron):**

```bash
# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 lives in .claude/settings.json (engineering.md §9).
claude --teammate-mode in-process \
       --dangerously-skip-permissions \
       -p "Bring up the trading-team per .claude/teams/trading-team.spec.json.
           Run one decision cycle per trading.md §5.
           On completion, run 'clean up the team' and exit."
```

`--dangerously-skip-permissions` is unavoidable for unattended operation. Safety in this mode comes **exclusively** from `engineering.md §1` risk gates, `engineering.md §3` capital gate, `engineering.md §2` kill switch, and `engineering.md §9` hooks — the permission system is no longer a defense layer. Blast radius of any one cycle is bounded by these gates plus the cycle's ~12-min lifetime.

**Operational pre-conditions (binding):**
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` in `.claude/settings.json`.
- Claude Code version pinned in `infra/` (e.g. via Docker image digest or a pinned `.claude-code-version` file). Floating `latest` tags are forbidden — a Claude Code release can change Agent-Teams behavior.
- The cycle's spawn prompt instructs the Lead to call `clean up the team` before exit. The `Stop` hook (`engineering.md §9`) asserts cleanup happened.
- The next cycle's `SessionStart` hook (`engineering.md §9`) sweeps any stale `~/.claude/teams/{team-name}/` directory left by a crashed prior cycle before spawning the new team.
- Team config (`~/.claude/teams/{team-name}/config.json`) is runtime state, written by Claude Code, **never manually edited** (Cloud-doc warning). The team-spec source-of-truth lives in `.claude/teams/trading-team.spec.json` in the repo and is read at boot time.

**Hooks** (full set in `engineering.md §9`):
- Production-cycle: `SessionStart` (janitor), `TaskCreated` (schema-validate), `TaskCompleted` (artifact-validate via Pydantic), `Stop` (cleanup-assertion).
- Dev-session hooks fire here too because hooks are global to the `.claude/settings.json`.

**Cloud-doc constraints** (all trivially satisfied):
- Subagents may not spawn further teams — irrelevant in MVP, no member uses subagents.
- Lead is fixed for the team's lifetime — and the team's lifetime is exactly one cycle, so this is moot.
- No nested teams — no member tries to bring up a sub-team.
- No session resumption — every cycle is a fresh session by construction.
- One team per session — each `claude` process owns exactly one team.

**Versioning.** Team-spec changes (member roster, system prompts, subagent definitions) go through normal PRs against `.claude/agents/` and `.claude/teams/trading-team.spec.json` (`engineering.md §8` branch protection). If a Claude Code release breaks the team, exactly one cycle fails before the operator pins back to a known-good version; paper-mode catches breaking changes before real_capital cycles see them.

---

## 3. Memory Split

Two layers in MVP. Short-term lives in the cycle's process and dies with it; long-term lives in Postgres and is the only coupling between cycles.

| Scope | Mechanism | Lifetime | Read by |
|---|---|---|---|
| **Short-term coordination** | Shared task list + mailbox (Lead-managed); in-flight Pydantic artifacts in process memory | one cycle | Lead + members of the same cycle |
| **Short-term reasoning** | Per-member context window | one cycle (member shutdown) | Owning member only |
| **Long-term episodic** | `predictions`, `decisions`, `trades`, `paper_trades`, `positions` | indefinite | All future cycles + lessons-summary script + outcome-ingestion script |
| **Long-term operational** | `cycle_plan` (single active row), `notes` (LRU), `system_state` (key/value) | cross-cycle | Next cycle's Lead at boot; outcome-ingestion script for `last_outcome_ingestion_at`; trading-agent for `notes` |
| **Long-term reflective** | `lessons` (status='open') | indefinite | Trading-agent's prompt critical-learning section (top-K) |

The short-term layer is **never** read by future cycles — it's discarded at cleanup. The long-term layer is **never** used for in-cycle coordination — it carries only what future cycles need. Crossing this boundary requires an explicit Postgres write inside the cycle (e.g. `notes` updates from the trading-agent, `cycle_plan` synthesized by the Lead at cycle close).

Schemas: `data_infrastructure.md §1`.

---

## 4. Service View (MVP)

```
                            ┌──────────────────┐
        cron 12 min   ────► │  Trading Cycle   │ ──► claude --dangerously-skip-permissions
                            │  (Agent Team:    │     (Lead + 3 members; trading.md §2)
                            │   Lead + 3)      │
                            └────┬─────────────┘
                                 │ role=trading_cycle
                                 │ SELECT all + INSERT/UPDATE on
                                 │ predictions, decisions, trades,
                                 │ paper_trades, positions,
                                 │ cycle_plan, notes, system_state
                                 ▼
                            ┌──────────────────────────────────────────┐
                            │              Postgres 16                 │
                            │     (single storage tier per             │
                            │      data_infrastructure.md MVP scope)   │
                            └────────▲─────────────────────────▲───────┘
                                     │                         │
                       role=outcome_ingestion         role=lessons_summary
                       SELECT all + UPDATE only       SELECT trading +
                       on outcome / realized_pnl /    INSERT only on lessons
                       status / last_outcome_at       │
                                     │                         │
                            ┌────────┴──────────┐    ┌─────────┴─────────┐
        cron 5–10 min ────► │ Outcome Ingestion │    │  Lessons Summary  │ ◄──── cron daily
                            │ (Python script)   │    │  (Python script)  │
                            │ trading_feedback. │    │  optimization.md  │
                            │       md §1       │    │        §1         │
                            └────────┬──────────┘    └───────────────────┘
                                     │
                                     ▼
                            Polymarket Gamma API (read-only, resolved markets)
```

Three processes, three Postgres roles, no shared in-process state. The Polymarket CLOB write surface is touched **only** by the Trading Cycle's `risk-execution` member (and only when `TRADING_MODE=real_capital`). Outcome Ingestion talks only to Polymarket Gamma read-only.

---

## 5. Versioning & Pin

- **Claude Code version pinned** in `infra/` (e.g. `infra/.claude-code-version` or pinned in a Docker base image). No floating `latest` tags.
- **Team-spec source-of-truth:** `.claude/teams/trading-team.spec.json` (committed). Never edit `~/.claude/teams/{team-name}/config.json` directly — that's runtime state, regenerated each cycle.
- **Member definitions:** `.claude/agents/{scanner-reviewer,trading-agent,risk-execution}.md`. Per-member system prompt + tool allow-list.
- **Spec changes** = PRs against the above three locations; `engineering.md §8` branch protection applies. If a change touches `risk/` or `MAX_CAPITAL_EUR`, the ≥ 2-human rule kicks in.

---

## 6. Failure Handling (MVP)

| Failure | Behavior |
|---|---|
| Cycle process crashes / cron miss | Non-event. No orders placed. Next scheduled tick runs cleanly. Idempotency on the order side (`engineering.md §1` solvency gate + internal idempotency keys) prevents duplicate orders if a partial run resumes. |
| Lead hangs (e.g. Anthropic API outage) | Killed by scheduler timeout (`cron` + `timeout` wrapper, default cycle period + 60s grace). Stale `~/.claude/teams/` left behind is swept by the next cycle's `SessionStart` janitor. |
| Member returns malformed artifact | `TaskCompleted` hook (`engineering.md §9`) Pydantic-validates and forces retry on schema mismatch. After repeated failure, the cycle aborts; no orders placed; next cycle runs. |
| Outcome-ingestion or lessons-summary script crashes | Non-fatal. Idempotent, picks up from `system_state.last_outcome_ingestion_at` (or equivalent for lessons). Next cron tick runs. |
| Postgres outage | All three processes fail fast on connect. Operator alerted via standard structured-log alerting (`data_infrastructure.md §3`). No state corruption. |

There is no automatic recovery beyond "next cron tick"; that is by design — it keeps the runtime topology small and the failure surface visible.

---

## 7. Weiterer Ausbau (post-MVP)

Deferred until MVP is stable. Each item is a future expansion of one of the §1–§6 sections; consistent with `engineering.md §13`.

### Three-team architecture (extends §1)
- **Trade Evaluation Team** (Tier 1) as a Claude Code Agent Team running every 1 min — replaces today's MVP outcome-ingestion script with `evaluator` + 3 subagents (`outcome-fetcher`, `pnl-aggregator`, `agent-performance-updater`). Owned by `trading_feedback.md §6 Weiterer Ausbau`.
- **Code Evaluation Team** (Tier 2) as scheduled hourly / daily / weekly Claude Code Agent Team batches — replaces today's MVP daily lessons-summary script with the 6-role roster (`risk-auditor`, `pattern-miner`, `strategy-optimizer`, `strategy-explorer`, `prior-art-scout`, `meta-reviewer`). Owned by `optimization.md §5 Weiterer Ausbau`.
- These are added when the operator-manual-review loop has accumulated enough signal to justify automation, and only then.

### Multi-agent ensemble inside the Trading Team (extends §2)
- 7-persona heterogeneous roster (`base-rate-bayesian`, `news-synthesizer`, `domain-router`, `historical-analogue`, `contrarian-skeptic`, `microstructure-reader`, `red-team-adversary`) — owned by `trading.md §8 Weiterer Ausbau`.
- Aggregator member combining per-agent `p_raw` into `p_consensus` with disagreement metric.
- Per-agent capital allocation managed by `meta-allocator` (post-MVP service).

### Subagent fan-out per member (extends §2)
- `web-searcher`, `news-fetcher`, `analogue-finder`, `related-market-scanner` as one-way fan-out subagents inside individual members for I/O-bound parallel research.
- `domain-router` per-domain sub-prompts (`politics-sub`, `crypto-sub`, `sports-sub`, `macro-sub`).
- `safety-watchdog` with `reconciliation-diff-explainer` subagent on triggered alerts.

### Multi-cadence loops (extends §1)
- 1s snapshot loop for active-position orderbooks.
- 30s reconciler matching internal state to broker truth (`data_infrastructure.md §2`).
- 24h capital-allocation rebalance.
- Hourly / daily / weekly Code-Evaluation batches.

### `meta-allocator` capital-allocation feedback (replaces and extends today's §3 — fully removed in MVP form)
- Deterministic Python service rebalancing per-agent capital weekly based on rolling 30d hit rate + PnL (softmax, T=0.5).
- Adaptation-quality bonus capped at +10% — share of an agent's `lessons` that became merged proposals with positive paper outcome.
- Output `agent_state.allocation_pct` consumed by `risk-engine` for per-agent sizing caps.
- Irrelevant in MVP because there's only one trading agent.

### Richer memory layers (extends §3)
- 10-layer memory taxonomy: agent `beliefs` (typed views with revision lineage), `operating_doctrine` (multi-phase strategy), position-thesis beliefs, `patterns`, `proposals`, LTKDs.
- Per-agent `notes` partitioning (today: single shared `notes` table because single agent).

### Off-cycle teams (extends §1)
- Parallel debugging on alerts — incident-response spawns ad-hoc teams for competing-hypothesis investigation.
- PR review team — `security-reviewer` + `risk-reviewer` + test-runner as a 3-member team.
- Backtest fan-out — one teammate per candidate strategy. (Backtest itself is out of scope for v1 per `engineering.md §4` / `trading.md`.)

### `safety-watchdog` long-running service (extends §6)
- Independent of the cycle, deterministic Python service in `risk/`, watches for ≥ 3 consecutive cycle-failures.
- On trip: sets system to monitor-only mode until recovery; existing positions remain governed by deterministic rules in `risk/` (stop-outs, kill-switch, time-based close).
- Today (MVP): no auto-monitor-only mode; the operator decides what to do after a failed cycle.

### Hook + permission expansions (extends §2)
- `TeammateIdle` hook — if member idles 3× on the same task, abort cycle and emit alert (no kill-switch trip — single bad cycle is non-fatal). Today's MVP set in `engineering.md §9` does not include this.
- Permission-mode boundaries refined per off-cycle team (read-only Tier-1, no-CLOB Tier-2, etc.).

### Versioning + rollback intricacies (extends §5)
- Image-digest pinning (Docker) instead of pinned-version-file.
- Automated rollback on N consecutive cycle failures after a Claude Code upgrade.

---

## See also

- `trading.md §2` — Trading Team member topology, single-agent context.
- `trading_feedback.md §1` + §2 — Outcome-Ingestion script + authority boundary.
- `optimization.md §1` + §2 — Lessons-Summary script + authority boundary.
- `engineering.md §9` — full hook set (dev + production-cycle).
- `engineering.md §13` — master post-MVP deferral list (this file's §7 is consistent with it).
- `data_infrastructure.md §1` — schemas (`predictions`, `decisions`, `trades`, `paper_trades`, `positions`, `cycle_plan`, `notes`, `lessons`, `system_state`).
- `engineering.md §4` — `TRADING_MODE` flag governance.
- `specs.md` — architecture diagram and entry point.
