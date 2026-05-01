# Orchestration — Teams, Scheduling, Capital Allocation

How the system runs at runtime. Owns: the three-team architecture, memory split between short-term and long-term, agent-team topology (Lead + Members + Subagents), agent-team production-runtime mechanics (bootstrap, scheduler, cleanup, team hooks), and the per-agent capital-allocation feedback loop.

**What lives here:** runtime topology and lifecycle. **What does not live here:** schemas (`data_infrastructure.md §1`), risk limit values (`engineering.md §1`), execution mechanics (`data_infrastructure.md §2`), repository conventions and CI hooks (`engineering.md §3`+).

---

## 1. System Architecture

**Three independent teams**, each running as its own Claude Code Agent Team in its own scheduled `claude` process. They never share a session, only the long-term memory layer (`data_infrastructure.md §1`). Decoupled scheduling is the point — a stuck team cannot block the others.

| Team | Schedule | Purpose | Writes to | Reads |
|---|---|---|---|---|
| **Trading Team** (`trading.md`) | every 12 min | Decide which markets to trade and place orders | `predictions`, `decisions`, `trades`, `positions`, `cycle_plan`, `notes`, `beliefs` | everything |
| **Trade Evaluation Team** (Tier 1, `trading_feedback.md`) | every 1 min on resolution event | Score the past: when a market resolves, compute outcome, PnL, per-agent hit-rate; mark the corresponding position closed; emit raw observations | `predictions.outcome`, `trades.realized_pnl`, `positions` (closed), `agent_performance` | resolved markets, open positions |
| **Code Evaluation Team** (Tier 2, `optimization.md`) | hourly / daily / weekly batches | Score the system: read Trade-Evaluation outputs and trading memory, propose code/prompt/strategy/agent-roster changes, **open PRs for human review** — never self-merge | `lessons`, `patterns`, `proposals`, Git PRs on feature branches | everything except live-trading endpoints |

**Per-cycle pipeline (Trading Team).** Sequence of tasks coordinated by the **Trading-Team Lead**, executed by **Team-Member Agents** (each its own context window), some of which spawn **Subagents** for parallel sub-tasks. Memory passed as typed artifacts and persisted (fully replayable):

```
[Trading-Team Lead]         → drives cycle clock; spawns members; assigns + monitors tasks; synthesizes
        ↓
[market-scanner]            → universe of all tradable markets (PA-style: no filter)
[portfolio-reviewer]        → PortfolioState  (positions, cash, PnL, last 10 settlements + trades)
[trading agents]            → Prediction      (p_raw, reasoning) per agent  ← call research tools (web search, news, analogues) inline as needed
[aggregator]                → ConsensusProbability  (mean of p_raw, with disagreement metric)
[risk-engine]               → Decision        (action, size, gate results, rationale)
[execution-engine]          → Trades + fills (paper or real-capital, `engineering.md §11`)
```

The four-stage shape — **Receive (market-scanner) → Review (portfolio-reviewer) → Analyze (agents call research tools, then aggregator) → Decide (risk-engine + execution)** — mirrors the canonical Prediction Arena cycle, generalized to a multi-agent ensemble. Review precedes Analyze deliberately: the portfolio snapshot is consumed by both the agent prompts and the risk gates, so it must exist before either runs.

The **`evaluator` is no longer a Trading-Team member** — it is the Trade Evaluation Team (`trading_feedback.md`), running on a separate 1-minute schedule because market resolutions arrive asynchronously to trading cycles (UMA oracle, hours to days after a market closes).

**Cross-cycle reflection.** The Code Evaluation Team reads the outputs of the other two teams and proposes structured changes — never to live state, always as PRs that the operator reviews and merges:

```
predictions ┐
decisions   ├──→  lessons  ──→  patterns  ──→  proposals  ──→  Git PRs (operator reviews and merges)
trades      │    (append)      (curated)      (gated)
outcomes    ┘
```

**Agent-Team topology (Lead + Members + Subagents).** The trading loop is implemented as **one Claude Code Agent Team per cycle** (`code.claude.com/docs/en/agent-teams`) — the experimental feature is the production runtime. Every cycle, a scheduler fires a fresh `claude` process; the Lead boots the team, runs one cycle, cleans up, exits. Long-term memory persists across cycles in Postgres / S3 (`data_infrastructure.md §1`); short-term memory (mailbox, task list, member context windows) lives only inside one cycle's process and dies with it.

**Why fresh-team-per-cycle (vs. one persistent team running 24/7):** the design rule *"Memory is the only coupling between tasks"* is **enforced by construction** rather than by trusting a long-lived Lead to reset member contexts correctly. A bad cycle cannot poison the next; memory leaks are physically impossible; Claude Code version upgrades pick up at the next cycle naturally; the Cloud-doc limitations (no session resumption, fixed Lead, one team per session) all become non-issues because every cycle starts a fresh session anyway. Cost: a one-off boot of ~5–30s per cycle, which is < 5% of a 12-minute cycle period and happens entirely before edge-time-sensitive work begins.

**Operational pre-conditions** (binding):
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` set in `.claude/settings.json` (`engineering.md §6`).
- Claude Code v2.1.32 or later, pinned in `infra/` (§2 of this file).
- Each cycle's Lead process is started with `--dangerously-skip-permissions` so it does not block on interactive permission prompts during unattended operation. Safety in this mode comes **exclusively** from `engineering.md §1` risk-gates, `engineering.md §2` kill-switch, and `engineering.md §6` hooks — the permission system is no longer a defense layer (see `engineering.md §15`). Blast radius of any one cycle is bounded by these gates.
- The cycle scheduler (cron / k8s `CronJob` / `/loop` skill) fires every cycle period (12 min, set in central settings `engineering.md §21`). A missed or failed cycle is a non-event: no team is started, no orders are placed, the next scheduled cycle simply runs. There is no supervisor with liveness probes, no auto-restart logic — the cron is the supervisor.
- The Lead **must** call `clean up the team` before process exit (Cloud-doc warning). Any cycle process that crashes without cleanup leaves a stale `~/.claude/teams/{team-name}/` directory; a janitor step at the start of the next cycle (§2) sweeps stale entries before spawning the new team.
- Team config (`~/.claude/teams/{team-name}/config.json`) is runtime state, written by Claude Code, never manually edited (Cloud-doc warning). The team-spec source-of-truth lives in `.claude/teams/trading-team.spec.json` in the repo and is read at boot time.

**Topology:**

```
┌────────────────── Team Lead Agent (per cycle) ──────────────────┐
│  - drives cycle clock; initializes shared task list              │
│  - spawns members at the right time, passes typed artifacts      │
│  - consumes the risk gates' results to short-circuit if blocked  │
│  - synthesizes intermediate results + member disagreements       │
│  - emits cycle-close artifacts; never executes orders directly   │
└──────────────────────────────────────────────────────────────────┘
                              │
        ┌─── shared task list + mailbox (short-term) ───┐
        ▼                                                ▼
[Trading-Team Members]                          [each in own context window]
  · market-scanner
  · portfolio-reviewer
  · trading-agent-{id} (×7)   ─┐ each agent calls research tools (web_search,
  · aggregator                 │ news_fetch, analogue_lookup, related_market_scan)
  · risk-engine                │ inline during inference; may spawn Subagents
  · execution-engine          ─┘ for I/O-bound fan-out (one-way, parent-only)
```

**Member Subagents** (focused, fan-out only — *no* mailbox, *no* shared task list, results return to parent member only):

| Team member | Subagents (typical) |
|---|---|
| Any trading agent | `web-searcher`, `news-fetcher`, `analogue-finder`, `related-market-scanner` — fanned out for I/O-bound research on the markets the agent chooses to focus on |
| `domain-router` (trading agent) | Per-domain sub-prompts (`politics-sub`, `crypto-sub`, `sports-sub`, `macro-sub`) so the parent context stays lean |
| `safety-watchdog` | `reconciliation-diff-explainer` (only on triggered alerts) |
| `evaluator` (Trade Evaluation Team) | `outcome-fetcher`, `pnl-aggregator`, `agent-performance-updater` |

**Cloud-doc constraints (binding because the feature is the runtime):**
- Subagents may not spawn further teams (only the Lead manages the team). They may fan out into nested subagents within their own session (e.g. one `web-searcher` per query) — this is the right primitive for parallelizable I/O-bound work.
- Lead is fixed for the team's lifetime — and the team's lifetime is exactly one cycle, so this constraint is no longer load-bearing.
- No nested teams: a member cannot bring up its own sub-team. If a member needs hierarchical coordination, it must do so via subagents only.
- No session resumption: irrelevant in this design, since every cycle starts a new session by construction.

**Why members vs subagents:** members coordinate (mailbox + shared list, can challenge each other, peer-visible); subagents fan out (parent-only return, cheaper because results summarize back into the parent's context). Same rule as in the Cloud doc — choose by whether workers need to talk to each other.

**Memory split: short-term (per cycle) vs long-term (across cycles).** Every cycle, the Team Lead provisions a fresh short-term layer that is discarded at cycle close. Long-term memory persists across cycles as the durable record of the system's accumulated knowledge.

| Scope | Mechanism | Lifetime | Read by |
|---|---|---|---|
| **Short-term coordination** | **Shared task list** (per-cycle, file-locked, states `pending`/`in_progress`/`completed`, with explicit dependencies — e.g. `aggregation` blocks on `agent-inference-complete`) | one cycle | Lead + all members of the same cycle |
| **Short-term coordination** | **Mailbox** (auto-delivered messages between members) | one cycle | Sender + named recipient(s) |
| **Short-term hand-off** | **In-flight typed artifacts** in process memory: `PortfolioState`, `Prediction`, `ConsensusProbability`, `Decision` | one cycle (persisted on close to `data_infrastructure.md §1` for replay only) | Downstream members of same cycle |
| **Short-term reasoning** | **Per-member context window** | one cycle (member shutdown) | Owning member only |
| **Long-term agent state** | `notes` (LRU scratchpad) | cross-cycle, capped | Owning agent's next cycle; Code-Evaluation agents |
| **Long-term agent state** | `beliefs` (typed, revisable with lineage) | cross-cycle, indefinite | Owning agent's next cycle; Code-Evaluation agents |
| **Long-term operational** | `cycle_plan` (single-row latest, forward-looking handoff) | overwritten each cycle (history kept) | Next cycle's Team Lead at boot |
| **Long-term operational** | `operating_doctrine` (active phased strategy with target date) | revisable, lineage kept | Trading agents via prompt; `strategy-optimizer` for revision |
| **Long-term episodic** | `predictions`, `decisions`, `trades`, `positions` | indefinite | All future cycles + Code-Evaluation agents |
| **Long-term reflective** | `lessons` (append-only), `patterns` (curated), `proposals` (gated), LTKDs (quarterly) | indefinite | Code-Evaluation agents; trading agents via critical-learning section in prompt |

The short-term layer is **never** read by future cycles — it cleans up at cycle close. The long-term layer is **never** used for in-cycle coordination — it carries only what future cycles need. Crossing this boundary requires an explicit write into one of the long-term stores (e.g. an agent calling `manage_beliefs.create` or the Trade Evaluation Team's `evaluator` updating `predictions.outcome` on resolution).

**Memory taxonomy (10 layers, full detail):**

| Layer | Where | Lifetime | Purpose |
|---|---|---|---|
| Working / Short-term | Shared task list + mailbox (Lead-managed); in-flight Pydantic artifacts in process memory; per-member context window | one cycle | Inter-member coordination + hand-offs within a cycle |
| Agent notes | Per-agent `notes` table (max 50 × ~200 words, LRU) | cross-cycle, capped | Trading agent's scratchpad — ad-hoc reminders + provisional flags |
| Agent beliefs | Per-agent `beliefs` table, typed by domain | cross-cycle, revisable, full lineage | Structured market views — hit-rate-tracked when falsifiable |
| Cycle plan | `cycle_plan` table, single active row, portfolio-level | overwritten each cycle, history retained | Forward-looking handoff: next-cycle priorities, holds-with-rationale, pending settlements, blockers |
| Operating doctrine | `operating_doctrine` table, single active row with lineage | revisable (weeks–months), lineage retained | Currently-active phased strategy with target date — directive, not retrospective |
| Episodic | `predictions`, `decisions`, `trades`, `positions` | indefinite | Per-event ground truth; replay/audit |
| Reflective | `lessons` | indefinite (compacted) | Observations + hypotheses by Code-Evaluation agents |
| Pattern | `patterns` | indefinite (curated) | Recurring observations clustered from lessons |
| Proposal | `proposals` | indefinite | Pending/accepted/rejected change requests |
| Long-term | LTKDs (`optimization.md §5`) | indefinite (revised quarterly) | Compressed background context for Code-Evaluation-agent prompts |

**Design implications:**
- Memory is the only coupling between tasks → swapping any agent is local.
- Within the prediction stage, trading agents do **not** share memory in real time (`trading.md §2` independence). Diversity is the value there. Orchestration applies *between* stages, not within parallel prediction.
- Reflection is asynchronous + human-gated (`optimization.md §7`). Trading hot path never waits on Code-Evaluation agents.
- Every artifact has a Pydantic schema (`engineering.md §13`). No untyped dicts cross task boundaries.

**Layered service view.** Four logical layers, services on async message bus, isolated failure + independent scaling.

```
┌─────────────────────────────────────────────────────────────┐
│   Meta:  Capital Allocator | Evaluator | Observability | Safety
│   Decision:  Agent Pool (N) → Aggregator → Risk Gates
│   Data:  Polymarket | News | Social | Web | Historical | TS DB
│   Execution:  Order Manager | Position Tracker | Reconciler | CLOB
└─────────────────────────────────────────────────────────────┘
```

**Component services:**

| Service | Responsibility |
|---|---|
| `data-ingestor-{source}` | Pull data from each external source |
| `market-scanner` | Filter universe by liquidity / edge potential |
| `portfolio-reviewer` | Snapshot positions, cash, unrealized + realized PnL, last 10 settlements, last 10 closed trades; emit `PortfolioState` consumed by agent prompts and risk gates |
| `agent-worker-{id}` | Run one agent's reasoning loop in isolation; agent calls research tools (web search, news fetch, analogue lookup) inline as needed |
| `aggregator` | Combine per-agent outputs into consensus |
| `risk-engine` | Apply pre-trade risk gates |
| `execution-engine` | Translate decisions into CLOB orders (or paper-trading ledger writes) |
| `position-manager` | Maintain real-time portfolio state |
| `reconciler` | Cross-check internal state vs broker truth |
| `meta-allocator` | Per-agent capital weights |
| `evaluator` (Trade Evaluation Team) | Compute outcome + PnL + per-agent hit-rate on resolved markets; runs on its own 1-min schedule |
| `safety-watchdog` | Enforce kill switch, drawdown, sanity limits |

**Design principles:**
- No agent/service is a single point of failure.
- Stateless workers where feasible; durable state in Postgres/Redis.
- Decisions immutable once persisted.
- Side effects (orders) idempotent via internal keys.
- Every external call wrapped in a circuit breaker.

---

## 2. Agent Teams Production Runtime

**Production trading loop = one fresh Claude Code Agent Team per cycle.** Every cycle (period from central settings, `engineering.md §21`, default 12 min) the scheduler fires a new `claude` process. The Lead boots the team, runs the cycle (`trading.md §5`), calls `clean up the team`, and exits. The next scheduled cycle starts a brand-new process with no in-process state from the previous one. Long-term memory persists in Postgres / S3; short-term memory dies with the process. This makes the §1 *"memory is the only coupling between tasks"* invariant **enforced by construction**, not by trusting a long-lived Lead to reset state correctly.

**Why per-cycle (rationale):**
- Bug isolation: a misbehaving cycle cannot poison the next.
- Memory leaks impossible (process dies).
- Cloud-doc limitations of the experimental feature (no session resumption, fixed Lead, one team per session) become non-issues — every cycle is a fresh session by design.
- Claude Code version upgrades are picked up automatically at the next cycle. If an upgrade breaks the team, exactly one cycle fails and the next one runs the previous (pinned) version after rollback.
- Failure handling is trivial — a failed cycle does not run; the next one tries again. No supervisor with liveness probes, no auto-restart logic.

**Bootstrap (single command per cycle, run by scheduler, not by a human):**

```bash
# .claude/settings.json sets CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 (engineering.md §6).
# Run by cron / k8s CronJob / `/loop` skill.
claude --teammate-mode in-process \
       --dangerously-skip-permissions \
       -p "Bring up the trading-team per .claude/teams/trading-team.spec.json.
           Run one decision cycle per trading.md §5.
           On completion, run 'clean up the team' and exit."
```

**Scheduler choice:**
- **Production / paper-mode**: `cron` on the dedicated server, or k8s `CronJob` with `activeDeadlineSeconds` set to (cycle-period + 60s grace). Concurrency policy = `Forbid` so a slow cycle never overlaps with the next start.
- **Local dev / smoke-runs**: the `/loop` skill (`/loop 12m run-trading-cycle`) for hands-on iteration without setting up cron.
- The scheduler is the supervisor. A failed cycle is a non-event — no orders are placed, the next fire-time runs cleanly.

**Cleanup discipline.** The Cloud-doc is explicit: *"Always use the lead to clean up. Teammates should not run cleanup."* Mechanisms to enforce this:
1. The cycle's spawn prompt instructs the Lead to call `clean up the team` before exit.
2. The `Stop` hook (`engineering.md §6`) asserts cleanup was called; if not, forces it before allowing process exit.
3. The next cycle's `SessionStart` hook (`engineering.md §6`) sweeps any stale `~/.claude/teams/` directories left by a crashed prior cycle, before spawning the new team.

**Hooks specifically for the team** (in `.claude/settings.json`, see `engineering.md §6` table for full set):
- `SessionStart` — janitor + version + spec checks before team spawn.
- `TeammateIdle` — keep members working until artifact written; abort cycle after 3 idle escalations on the same task (single bad cycle is non-fatal, no kill-switch trip).
- `TaskCreated` — schema-validate tasks before claim.
- `TaskCompleted` — Pydantic-validate output artifacts before marking complete.
- `Stop` — assert + force cleanup before exit.

**Subagents** (one-way fan-out, parent-only return — `code.claude.com/docs/en/sub-agents`): used by individual members for focused work where peer dialogue is not needed. Subagent definitions in `.claude/agents/` are reusable as both delegated subagents and team members. Examples per member listed in §1.

**Off-cycle teams.** Same Claude Code feature, separate processes, separate `team-name`:
- **Trade Evaluation Team** (`trading_feedback.md §2`) — runs every 1 min on its own cron; ingests resolved markets and writes ground-truth fields. Single-member team, cleans up after each tick.
- **Code Evaluation Team batches** (`optimization.md §2`, `optimization.md §5`) — scheduled separately from the trading cycle; daily/weekly batches each run their own Code-Evaluation Team session, clean up, exit.
- **Parallel debugging on alerts** — incident response spawns ad-hoc teams for competing-hypothesis investigation.
- **PR review** — security-reviewer + risk-reviewer + test-runner as a 3-member team.
- **Backtest fan-out** — one teammate per candidate strategy.

The "one team per session" Cloud-doc limit holds trivially since each session is one process and each process owns one team.

**Decision rule (when to use what):**

| Situation | Use |
|---|---|
| Focused task, only the result matters, no peer dialogue | Subagent |
| Workers must compare / challenge / coordinate | Agent Team |
| Inside a member, focused fan-out (multiple queries, parallel scans) | Subagent (within member session) |
| Production trading cycle | The Trading Team (one fresh team per cycle, this section) |
| Off-cycle improvement / debug / review | Separate Agent Team in its own Claude Code process |

**Versioning & rollback.** Claude Code version pinned in `infra/` (e.g. via Docker image digest, not a floating tag). Team-spec source-of-truth (member roster, subagent definitions, system prompts) lives in `.claude/agents/` + `.claude/teams/trading-team.spec.json` in the repo. The runtime `config.json` written by Claude Code is ephemeral per cycle and not edited by hand (Cloud-doc rule). Any change to the team spec is a normal PR through `engineering.md §5` branch protection. If a Claude Code release introduces an Agent-Teams-breaking change, exactly one cycle fails before rollback; paper-mode catches this before prod via the upgrade-validation cycles run there.

---

## 3. Capital-Allocation Feedback

`meta-allocator` (deterministic Python service, not a Claude team) rebalances per-agent capital weights weekly based on:
1. **Trading-performance** (rolling 30d hit rate + PnL — primary).
2. **Adaptation quality** — share of an agent's `lessons` that became merged proposals with positive paper outcome (small bonus, capped +10% of base allocation).

Rewards trading agents whose failure modes were genuinely informative — closes the loop between the Trading Team and the Code Evaluation Team. Allocation outputs are written to `agent_state.allocation_pct`, consumed by the `risk-engine` for per-agent sizing caps.

---

## See also

- `trading.md` — the trading runtime that this orchestration runs.
- `data_infrastructure.md` — schemas, prediction-market interface, observability.
- `engineering.md` — risk management, safety controls, hooks, secrets, central settings, tech stack.
- `trading_feedback.md` — Tier 1 evaluation team (the 1-min cron team).
- `optimization.md` — Tier 2 evaluation team (the daily/weekly batch teams).
- `specs.md` — architecture diagram and entry point.
