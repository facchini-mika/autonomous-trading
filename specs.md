# Polymarket Autonomous Trading System — Specifications

## 1. Objective

Deploy capital on Polymarket binary prediction markets to generate risk-adjusted returns by systematically identifying and exploiting probabilistic mispricings. A heterogeneous ensemble of AI agents produces calibrated probability estimates compared against market-implied probabilities to extract expected-value edge.

**Core pattern.** This is an **agent-orchestration system**, not a single LLM with plumbing. Specialized agents perform distinct tasks (research, prediction, aggregation, risk gating, execution, reflection), pass typed memory artifacts within a cycle (§2), and accumulate cross-cycle memory (§15). Decisions emerge from the pipeline; outcomes feed back into memory.

**Primary KPIs:**
- Net Sharpe > 1.5 (annualized, post fees + gas)
- Max drawdown < 25% of equity
- Net PnL > 0 monthly; positive trailing 90d return
- Edge consistency: no single category > 50% of PnL

**Out of scope (v1):** non-binary scalar markets, leverage beyond bankroll, market-making, externally-pooled capital.

---

## 2. System Architecture

**Tier 1 — Per-cycle pipeline.** Sequence of tasks coordinated by a **Team Lead Agent**, executed by **Team-Member Agents** (each its own context window), some of which spawn **Subagents** for parallel sub-tasks. Memory passed as typed artifacts and persisted (fully replayable):

```
[Team Lead Agent]           → drives cycle clock; spawns members; assigns + monitors tasks; synthesizes
        ↓
[market-scanner]            → universe of candidate markets
[portfolio-reviewer]        → PortfolioState  (positions, cash, PnL, drawdown, remaining capacity per scope)
[research-orchestrator]     → ResearchBundle  (news, history, related markets; in S3)   ← spawns subagents
[trading agents — §4]       → Prediction      (p_yes, confidence, reasoning) per agent  ← may spawn subagents
[aggregator]                → ConsensusProbability  (calibrated, with disagreement metric)
[risk-engine]               → Decision        (action, size, gate results, rationale)
[execution-engine]          → Trades + fills
[evaluator (on resolve)]    → Outcome + PnL + per-agent calibration update
```

The four-stage shape — **Receive (market-scanner) → Review (portfolio-reviewer) → Analyze (research + agents + aggregator) → Decide (risk-engine + execution)** — mirrors the canonical Prediction Arena cycle, generalized to a multi-agent ensemble. Review precedes Analyze deliberately: if portfolio state already triggers a §6 trip-wire or exhausts capacity, the cycle skips expensive research entirely.

**Tier 2 — Cross-cycle reflection (§15).** Improvement agents read Tier-1 memory asynchronously and emit higher-order memory that flows back via prompt/strategy/config updates after human approval. The improvement layer is its own **Improvement Team** (§15.1) — separate Team Lead, separate task list — running off-cycle from trading:

```
predictions ┐
decisions   ├──→  lessons  ──→  patterns  ──→  proposals  ──→  Git PRs / config / prompt updates
trades      │    (append)      (curated)      (gated)
outcomes    ┘
```

**Agent-Team topology (Lead + Members + Subagents).** The trading loop is implemented as **one Claude Code Agent Team per cycle** (`code.claude.com/docs/en/agent-teams`) — the experimental feature is the production runtime. Every cycle, a scheduler fires a fresh `claude` process; the Lead boots the team, runs one cycle, cleans up, exits. Long-term memory persists across cycles in Postgres / S3 (§8); short-term memory (mailbox, task list, member context windows) lives only inside one cycle's process and dies with it.

**Why fresh-team-per-cycle (vs. one persistent team running 24/7):** The §2 design rule *"Memory is the only coupling between tasks"* is **enforced by construction** rather than by trusting a long-lived Lead to reset member contexts correctly. A bad cycle cannot poison the next; memory leaks are physically impossible; Claude Code version upgrades pick up at the next cycle naturally; the Cloud-doc limitations (no session resumption, fixed Lead, one team per session) all become non-issues because every cycle starts a fresh session anyway. Cost: a one-off boot of ~5–30s per cycle, which is < 5% of a 12-minute cycle period and happens entirely before edge-time-sensitive work begins.

**Operational pre-conditions** (binding):
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` set in `.claude/settings.json` (§14.4).
- Claude Code v2.1.32 or later, pinned in `infra/` (§14.22).
- Each cycle's Lead process is started with `--dangerously-skip-permissions` so it does not block on interactive permission prompts during unattended operation. Safety in this mode comes **exclusively** from §6 risk-gates, §12 kill-switch, and §14.4 hooks — the permission system is no longer a defense layer (see §14.14). Blast radius of any one cycle is bounded by these gates.
- The cycle scheduler (cron / k8s `CronJob` / `/loop` skill) fires every cycle period (12 min, set in central settings §14.21). A missed or failed cycle is a non-event: no team is started, no orders are placed, the next scheduled cycle simply runs. There is no supervisor with liveness probes, no auto-restart logic — the cron is the supervisor.
- The Lead **must** call `clean up the team` before process exit (Cloud-doc warning). Any cycle process that crashes without cleanup leaves a stale `~/.claude/teams/{team-name}/` directory; a janitor step at the start of the next cycle (§14.22) sweeps stale entries before spawning the new team.
- Team config (`~/.claude/teams/{team-name}/config.json`) is runtime state, written by Claude Code, never manually edited (Cloud-doc warning). The team-spec source-of-truth lives in `.claude/teams/trading-team.spec.json` in the repo and is read at boot time.

**Topology:**

```
┌────────────────── Team Lead Agent (per cycle) ──────────────────┐
│  - drives cycle clock (§3); initializes shared task list         │
│  - spawns members at the right time, passes typed artifacts      │
│  - decides Review-gate short-circuit (§3 step 2)                 │
│  - synthesizes intermediate results + member disagreements       │
│  - emits cycle-close artifacts; never executes orders directly   │
└──────────────────────────────────────────────────────────────────┘
                              │
        ┌─── shared task list + mailbox (short-term) ───┐
        ▼                                                ▼
[Team Members]                                  [each in own context window]
  · market-scanner
  · portfolio-reviewer
  · research-orchestrator     ─┐
  · trading-agent-{id} (×7)    │ may spawn Subagents (one-way fanout, results
  · aggregator                 │ return to parent member only)
  · risk-engine                │
  · execution-engine          ─┘
  · evaluator
```

**Member Subagents** (focused, fan-out only — *no* mailbox, *no* shared task list, results return to parent member only):

| Team member | Subagents (typical) |
|---|---|
| `research-orchestrator` | `news-fetcher`, `web-searcher`, `historical-analogue-finder`, `related-market-scanner`, `social-sentiment-extractor` — one per research dimension, fired in parallel per candidate market |
| `domain-router` (trading agent) | Per-domain sub-prompts (`politics-sub`, `crypto-sub`, `sports-sub`, `macro-sub`) so the parent context stays lean |
| `historical-analogue` (trading agent) | `analogue-finder`, `analogue-weighter` |
| `risk-engine` | `slippage-simulator`, `correlation-cluster-checker` for compute-heavy gate evaluations |
| `evaluator` | `outcome-fetcher`, `pnl-aggregator` |
| `safety-watchdog` | `reconciliation-diff-explainer` (only on triggered alerts) |

**Cloud-doc constraints (binding because the feature is the runtime):**
- Subagents may not spawn further teams (only the Lead manages the team). They may fan out into nested subagents within their own session (e.g. one `web-searcher` per query) — this is the right primitive for parallelizable I/O-bound work.
- Lead is fixed for the team's lifetime — and the team's lifetime is exactly one cycle, so this constraint is no longer load-bearing.
- No nested teams: a member cannot bring up its own sub-team. If a member needs hierarchical coordination, it must do so via subagents only.
- No session resumption: irrelevant in this design, since every cycle starts a new session by construction.

**Why members vs subagents:** members coordinate (mailbox + shared list, can challenge each other, peer-visible); subagents fan out (parent-only return, cheaper because results summarize back into the parent's context). Same rule as in the Cloud doc — choose by whether workers need to talk to each other.

**Memory split: short-term (per cycle) vs long-term (across cycles).** Every cycle, the Team Lead provisions a fresh short-term layer that is discarded at cycle close. Long-term memory persists across cycles as the durable record of the system's accumulated knowledge.

| Scope | Mechanism | Lifetime | Read by |
|---|---|---|---|
| **Short-term coordination** | **Shared task list** (per-cycle, file-locked, states `pending`/`in_progress`/`completed`, with explicit dependencies — e.g. `agent-inference` blocks on `research-bundle-ready`) | one cycle | Lead + all members of the same cycle |
| **Short-term coordination** | **Mailbox** (auto-delivered messages between members, used for clarifications, partial-result hand-offs, aggregator disagreement-flagging) | one cycle | Sender + named recipient(s) |
| **Short-term hand-off** | **In-flight typed artifacts** in process memory: `PortfolioState`, `ResearchBundle`, `Prediction`, `ConsensusProbability`, `Decision` | one cycle (persisted on close to §8 for replay only) | Downstream members of same cycle |
| **Short-term reasoning** | **Per-member context window** | one cycle (member shutdown) | Owning member only |
| **Long-term agent state** | `notes` (LRU scratchpad, §4) | cross-cycle, capped | Owning agent's next cycle; improvement agents (§15) |
| **Long-term agent state** | `beliefs` (typed, revisable with lineage, §4) | cross-cycle, indefinite | Owning agent's next cycle; improvement agents |
| **Long-term operational** | `cycle_plan` (single-row latest, forward-looking handoff, §4) | overwritten each cycle (history kept) | Next cycle's Team Lead at boot |
| **Long-term operational** | `operating_doctrine` (active phased strategy with target date, §4) | revisable, lineage kept | Trading agents via §4 prompt; `strategy-improver` for revision |
| **Long-term episodic** | `predictions`, `decisions`, `trades`, `positions` (§8) | indefinite | All future cycles + improvement agents |
| **Long-term reflective** | `lessons` (append-only), `patterns` (curated), `proposals` (gated), LTKDs (quarterly) — §15.2/§15.3 | indefinite | Improvement agents; trading agents via critical-learning section in §4 prompt |

The short-term layer is **never** read by future cycles — it cleans up at cycle close. The long-term layer is **never** used for in-cycle coordination — it carries only what future cycles need. Crossing this boundary requires an explicit write into one of the long-term stores (e.g. an agent calling `manage_beliefs.create` or the evaluator inserting a row into `predictions`).

**Memory taxonomy (10 layers, full detail):**

| Layer | Where | Lifetime | Purpose |
|---|---|---|---|
| Working / Short-term | Shared task list + mailbox (Lead-managed); in-flight Pydantic artifacts in process memory; per-member context window | one cycle | Inter-member coordination + hand-offs within a cycle |
| Agent notes | Per-agent `notes` table (max 50 × ~200 words, LRU) | cross-cycle, capped | Trading agent's scratchpad — ad-hoc reminders + provisional flags |
| Agent beliefs | Per-agent `beliefs` table, typed by domain | cross-cycle, revisable, full lineage | Structured market views — calibration-tracked (§4 + §8) |
| Cycle plan | `cycle_plan` table, single active row, portfolio-level (§4) | overwritten each cycle, history retained | Forward-looking handoff: next-cycle priorities, holds-with-rationale, pending settlements, blockers |
| Operating doctrine | `operating_doctrine` table, single active row with lineage (§4) | revisable (weeks–months), lineage retained | Currently-active phased strategy with target date — directive, not retrospective |
| Episodic | `predictions`, `decisions`, `trades`, `positions` (§8) | indefinite | Per-event ground truth; replay/audit |
| Reflective | `lessons` (§15.2) | indefinite (compacted) | Observations + hypotheses by improvement agents |
| Pattern | `patterns` (§15.2) | indefinite (curated) | Recurring observations clustered from lessons |
| Proposal | `proposals` (§15.2) | indefinite | Pending/accepted/rejected change requests |
| Long-term | LTKDs (§15.3) | indefinite (revised quarterly) | Compressed background context for improvement-agent prompts |

**Design implications:**
- Memory is the only coupling between tasks → swapping any agent is local.
- Within the prediction stage, trading agents do **not** share memory in real time (§4 independence). Diversity is the value there. Orchestration applies *between* stages, not within parallel prediction.
- Reflection is asynchronous + human-gated (§15.4). Trading hot path never waits on improvement agents.
- Every artifact has a Pydantic schema (§14.12). No untyped dicts cross task boundaries.

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
| `portfolio-reviewer` | Snapshot positions, cash, unrealized + realized PnL, drawdown, daily loss, recent settlements; compute remaining capacity per category/agent/portfolio scope; emit `PortfolioState` and gate downstream (skip cycle if at trip-wire) |
| `research-orchestrator` | Bundle context per candidate market |
| `agent-worker-{id}` | Run one agent's reasoning loop in isolation |
| `aggregator` | Combine per-agent outputs into consensus |
| `risk-engine` | Apply pre-trade risk gates |
| `execution-engine` | Translate decisions into CLOB orders |
| `position-manager` | Maintain real-time portfolio state |
| `reconciler` | Cross-check internal state vs broker truth |
| `meta-allocator` | Per-agent capital weights |
| `evaluator` | Compute metrics on resolved markets |
| `safety-watchdog` | Enforce kill switch, drawdown, sanity limits |

**Design principles:**
- No agent/service is a single point of failure.
- Stateless workers where feasible; durable state in Postgres/Redis.
- Decisions immutable once persisted.
- Side effects (orders) idempotent via internal keys.
- Every external call wrapped in a circuit breaker.

---

## 3. Trading Loop

**Cycle period:** 12 min (configurable). Multiple time scales:

| Loop | Period | Function |
|---|---|---|
| Snapshot | 1 s | Update orderbook for active positions |
| Reconcile | 30 s | Match internal positions to broker truth |
| Decision | 12 min | Full scan → decide → execute |
| Recalibration | 1 h | Refit per-agent isotonic curves on recently resolved markets |
| Allocation | 24 h | Rebalance per-agent capital |
| Resolution | 1 min | Detect resolved markets, finalize PnL, feed calibration |

**Decision cycle (T = scheduler fire time, i.e. a fresh `claude` process started).** The four-stage spine is **Receive → Review → Analyze → Decide** (PA-pattern); a Boot phase runs first, and Cycle-close tears the team down. Steps below are the concrete sub-stages.

0. **T+0–15s — Boot.** Scheduler starts a fresh Claude Code process with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `--dangerously-skip-permissions`. A janitor first sweeps stale `~/.claude/teams/{team-name}/` directories left by a previous cycle that crashed without cleanup. Lead loads CLAUDE.md, MCP servers, hooks, the team-spec from `.claude/teams/trading-team.spec.json` (§14.22), then spawns members per the spec — each loads its subagent definitions and waits idle for tasks. Long-term memory pointers (current `MAX_CAPITAL_EUR`, latest calibration curves, recent settlement IDs) are read from Postgres and exposed to the Lead. The Lead also reads the **previous cycle's `cycle_plan`** (forward-looking handoff: priorities, holds, blockers) and the **active `operating_doctrine`** row (current phase + target date) from Postgres — both are propagated to members via the §4 prompt context. Boot completes when every member reports idle.
1. **T+15s — Receive: Universe snapshot.** Filter: 24h vol ≥ $10k; bid-ask depth ≥ $1k within ±1% of mid; end date > 24h and < 365d; not in cooldown.
2. **T+25s — Review: Portfolio & Performance.** `portfolio-reviewer` builds `PortfolioState`: current positions, cash, unrealized PnL, realized PnL (today, 7d, 30d), drawdown vs peak, daily loss vs cap, gross + per-category exposure, last 10 settlements + last 10 closed trades, rolling per-agent hit rate / PnL / Sharpe. Computes remaining capacity per scope against §6 limits. **Gate:** if any §6 trip-wire is active (15% drawdown kill-switch, 5% daily-loss cap, gross-exposure cap), the cycle either skips new orders entirely or restricts to position-reducing trades — research dispatch is short-circuited. The artifact is consumed by triage, risk-engine, and (subset) injected into agent prompts (§4).
3. **T+35s — Triage.** Cheap heuristic edge estimate (price vs base rate, related markets, momentum), filtered by remaining-capacity from `PortfolioState`. Top-K (K=20) advance.
4. **T+45s — Analyze: Research dispatch.** Per candidate: parallel bundle (news 7d, web search 3–5 queries, historical analogues, related-market snapshot). Persisted to S3, ID forwarded.
5. **T+3m — Analyze: Agent inference.** Each agent gets bundle + market metadata + relevant slice of `PortfolioState`. Output: `P(YES)`, confidence, reasoning trace URI. Parallel; per-agent timeout 90s. Failed agents excluded from this market only.
6. **T+5m — Analyze: Calibration & aggregation.** Per-agent isotonic recalibration; hit-rate-weighted mean. Consensus confidence = weighted geometric mean × disagreement penalty.
7. **T+6m — Analyze: Edge computation.** Live orderbook. Compute `q = mid`, `effective_q` = volume-weighted price for intended size, `edge = p_consensus − effective_q` (symmetric for NO).
8. **T+7m — Decide: Risk gates.** Order: edge threshold → position limits → category exposure → drawdown → slippage → resolution risk. Reject or size-reduce.
9. **T+8m — Decide: Sizing.** Quarter-Kelly within remaining caps from `PortfolioState`.
10. **T+9m — Decide: Order placement.** Limit, post-only when viable. Iceberg slicing for size > $500.
11. **T+10–11m — Fill monitoring.** Track partial fills. Cancel/replace on > 1% adverse move.
12. **T+11m45s — Persist & emit.** Lead persists in-flight artifacts (decisions, fills, reasoning links) to long-term stores (§8); writes any agent-initiated `notes` / `beliefs` / `lessons` updates; **writes a fresh `cycle_plan` row** (forward-looking handoff for the next cycle: top priorities, holds-with-rationale, pending settlements, identified-but-deferred opportunities, blockers) — synthesized by the Lead from the cycle's artifacts; emits cycle metrics. `operating_doctrine` is read-only here (only `strategy-improver` revises it, §15.1).
13. **T+11m55s — Cycle close.** Lead calls `clean up the team` (Cloud-doc warning: never let a member run cleanup), shutting down all members and the team config. Lead process exits. The next cycle is a brand-new `claude` process started by the scheduler at the next fire time — no shared in-process state with this cycle. All short-term memory (mailbox, task list, member context windows) dies with the process; only what was persisted in step 12 survives.

---

## 4. Agent Design

**Common contract.** Every agent implements:

```python
class Agent:
    id: str
    version: str
    prompt_template: str            # versioned, in repo
    tools: list[Tool]               # explicit allow-list

    def predict(self, market: Market, research: ResearchBundle) -> Prediction:
        """Returns Prediction(p_yes, confidence, reasoning_trace_uri,
                              sub_questions, key_evidence)"""
```

**Model.** All agents run on **Anthropic Claude Opus exclusively** (latest, pinned per release). Diversity comes from prompt × tool × persona, not from model heterogeneity. Trade-off: simpler ops/pinning/calibration/audit; no cross-provider drift; an Anthropic outage is a system-wide constraint (§9).

**Hard rule — cloud inference only (binding for all current and future versions).** Inference exclusively against managed cloud LLM endpoints. **No self-hosted, on-prem, locally-run, or edge-deployed inference** — not in v1, v2+, sub-component, research, shadow, or paper mode. Rationale: deterministic model-version provenance for replay, capability/safety tracking, vendor-managed eval pipelines, ops simplicity, no self-managed weight integrity risk. Future model additions (§13) must be cloud-hosted.

**Heterogeneous roster (initial 7).** Each is a distinct prompt × tool × persona configuration.

| ID | Style | Strength | Failure mode |
|---|---|---|---|
| `base-rate-bayesian` | Reference-class first, conservative updates | Anchors well, resists hype | Slow on genuine novelty |
| `news-synthesizer` | Recency-weighted news flow | Fast on news shocks | Overreacts to noise |
| `domain-router` | Routes to politics/crypto/sports/macro sub-prompts | Domain specificity | Misclassification on edges |
| `historical-analogue` | Finds 3–5 closest past events, weights outcomes | Empirical grounding | Reference-class fragility |
| `contrarian-skeptic` | Constructs case against consensus | Counters herd error | Forced contrarianism |
| `microstructure-reader` | Orderbook + flow + smart-money proxies | Catches positioning | Mistakes noise for signal |
| `red-team-adversary` | Tries to falsify the leading hypothesis | Robustness check | Excess uncertainty |

**Per-agent state:** predictions log (joined to outcome on resolution); calibration curve (isotonic, refit hourly on last 500 resolved); capital allocation (meta-allocator); internal portfolio (subset of global); performance history (rolling 7d/30d/90d Sharpe, hit rate, PnL).

**Independence guarantees:**
- Agents do not see each other's outputs before producing their own.
- Separate processes, independent LLM calls.
- Aggregation by a separate, deterministic service.

**Per-cycle prompt context** (assembled deterministically, on top of system prompt with role/philosophy/risk/tools/protocol). Most fields below are sourced from the upstream `PortfolioState` artifact built in §3 step 2 (Review) — the prompt does not re-query state, it consumes the already-snapshotted view, so all agents in a cycle see consistent numbers:
- Current timestamp + cycle metadata
- Market data: orderbook snapshot, settlement criteria, depth at ±1% of mid
- **Account state** (from `PortfolioState`): cash, positions, unrealized + realized PnL, drawdown vs peak, daily loss vs cap, gross + per-category exposure, remaining capacity per scope
- **Recent settlements** (from `PortfolioState`) — last 10 resolved markets with realized PnL
- **Recent closed trades** (from `PortfolioState`) — last 10 trades with realized PnL
- **Previous-cycle reasoning** — agent's own prior reasoning on same market/category
- **Previous cycle's `cycle_plan`** — operational handoff: priorities, holds-with-rationale, identified-but-deferred opportunities, blockers (e.g. settlement bottleneck). Read-only context for the agent.
- **Active `operating_doctrine`** — current phase, phase-specific actions, target date, key risks. Directive: agent's actions should be coherent with the current phase.
- **Critical-learning section** — curated `lessons`/`patterns` excerpts (§15.2): losing patterns to avoid, winning patterns to replicate, position-management reminders. Curated weekly by `meta-reviewer`; injected per cycle by research-orchestrator.
- The agent's most recent `notes`
- The agent's active `beliefs` for this market / category / open positions (top-K by relevance + recency)
- Step-by-step trading protocol

This is the explicit bridge between Tier-1 working memory and Tier-2 reflective memory. Every decision is conditioned on a defined, audit-inspectable slice of past experience.

**Agent notes (per-agent scratchpad).** Private `notes` store via `manage_notes` tool (read/write/search/edit). Bounded: max 50 × ~200 words, LRU. Used by the agent itself for patterns spotted during reasoning, reminders for next cycle, tentative hypotheses too provisional for `lessons`. Private to one agent (no cross-agent leakage during prediction — preserves §4 independence) but readable by improvement agents (§15) for pattern mining.

**Agent beliefs (structured market views).** Typed `beliefs` store via `manage_beliefs` tool (`create`, `revise`, `retire`, `search`). A belief is a structured statement the agent currently holds. Fields:
- `domain` — `market_structure` / `strategy` / `event` / `risk` / `sentiment`
- `scope` — `global` / `category` / `market` / `position` (a position scope binds the belief to an open position via `scope_ref = market_id` — the *thesis* for why we're holding it)
- `scope_ref` — category id, market id, or position market id depending on `scope`
- `statement` — short claim, ≤ 100 words
- `p_estimate` — optional probability for falsifiable beliefs (e.g. *"Fed pauses in December"* → P ≈ 0.7)
- `confidence` — agent's own confidence
- `evidence_uri` — pointer to grounding bundle/news/source
- `supersedes_id` — id of prior belief revised (lineage preserved; nothing deleted)
- `status` — `active` / `superseded` / `retired`

**Position-thesis beliefs (special case).** When a trading agent's prediction directly drives a position open, the execution-engine writes a `scope=position` belief alongside the `decisions` row, copying the agent's reasoning summary as the `statement` and the entry edge as `p_estimate`. On every subsequent cycle while the position is open, the per-cycle prompt for the originating agent injects this belief automatically; if the market price has moved > X% (default 15%, central settings §14.21) against the entry, the belief is auto-flagged for `revise` or `retire`. This closes the loop "why am I still holding this?" — every open position has a tracked, calibration-eligible thesis.

**Beliefs vs notes vs lessons vs predictions vs cycle_plan vs operating_doctrine:**

| Layer | Granularity | Author | Lifetime | Use |
|---|---|---|---|---|
| `predictions` (§8) | per-market, per-cycle | trading agent | event-bound | Direct probability now; consumed by aggregator |
| `notes` (§4) | free-form, per-agent | trading agent | LRU-capped | Ad-hoc scratchpad, no schema |
| `beliefs` (§4) | typed, structured, per-agent | trading agent | revisable, history kept | First-class views the agent operates *under* |
| `cycle_plan` (§4) | portfolio-level, single active row | Team Lead at cycle close | overwritten next cycle, history kept | Forward-looking handoff between cycles |
| `operating_doctrine` (§4) | portfolio-level, single active row with lineage | `strategy-improver` (proposed) + human (approved) | revisable, lineage kept | Currently in-force operative strategy |
| `lessons` (§15.2) | post-hoc observation | improvement agent | append-only | Reflective, written *about* the system |

**Belief usage:**
- Injected per cycle as *"your active beliefs about this market/category"* — top-K by relevance + recency.
- Falsifiable beliefs (with `p_estimate`) are performance-tracked: hit rate on belief outcomes alongside per-cycle predictions (§10).
- Settlement/move that contradicts an active belief auto-flags it next cycle; agent decides `revise` (new belief with `supersedes_id`) or `retire`.
- Improvement agents (§15) mine beliefs across the agent population for convergent/divergent views, surface stale beliefs, lift recurring true beliefs into shared `patterns`.

This makes the agent's *implicit world model* explicit, inspectable, and calibration-trackable.

**Cycle plan (forward-looking handoff).** Single-row portfolio-level artifact written at the very end of each cycle by the Team Lead, read by the *next* cycle's Lead at boot. Solves the gap that fresh-team-per-cycle (§2) creates: the next process knows nothing about what the previous one was about to do. Distinct from `notes` (per-agent, retrospective) and `lessons` (reflective, written by improvement agents). Fields:
- `written_at` / `written_by_cycle_id`
- `next_priorities` — ordered list of concrete actions for the next cycle
- `holds_with_rationale` — for each currently-open position: 1-line reason to hold rather than close
- `pending_settlements` — markets awaiting resolution that will free capital / trigger PnL recognition
- `opportunities_deferred` — opportunities identified this cycle but not actionable (e.g. capital locked) with the trigger condition under which to revisit
- `blockers` — operational constraints (e.g. capital lock from large open position, data-source outage)
- `superseded_at` — set when the next cycle writes its own plan (history retained for audit/replay)

The Lead synthesizes the plan from in-flight artifacts at step 12 (§3); it is not produced by an LLM call, it's a deterministic summarization of the cycle's `Decision`/`PortfolioState`/aggregator outputs (with one short LLM-written rationale field per priority/hold). Always exactly one active row.

**Operating doctrine (current operative strategy).** Single active row, revisable with full lineage (analogous to `beliefs`). Distinct from LTKDs (§15.3, retrospective background context for improvement agents) and `proposals` (§15.2, change requests). This is the **currently in-force operative strategy** that trading agents condition on. Fields:
- `target_date` — horizon the doctrine is written against
- `phases` — ordered list of phases, each with `name`, `entry_condition`, `exit_condition`, `actions`, `forbidden`
- `current_phase` — the phase active right now (re-evaluated each cycle by the Lead against the `entry_condition`/`exit_condition` of adjacent phases; transitions are logged to `decisions`-style audit)
- `key_risks` — risks the doctrine explicitly anticipates
- `revised_at` / `revised_by` — author and timestamp; lineage via `supersedes_id`
- `approved_by` — human approver (mandatory; revisions follow §15.4 review counts)

Revision authored exclusively by `strategy-improver` (§15.1) as a `proposal`; merging a doctrine `proposal` writes a new active `operating_doctrine` row and supersedes the prior. Trading agents and the Lead read but never write.

---

## 5. Decision Logic

**Per agent:**
- Raw output: `p_raw ∈ (0, 1)`
- Calibration: `p_cal = isotonic_i(p_raw)` from agent's last 500 resolved.
- Confidence `c ∈ (0, 1)`: from expected absolute calibration error at this `p_cal`.

**Aggregation:**
- `weight_i = max(hit_rate_i_30d − 0.5, 0.01)` — agents below random get a floor weight
- `p_consensus = Σ weight_i · p_cal_i / Σ weight_i`
- `confidence_consensus = (Σ weight_i · c_i / Σ weight_i) · (1 − std(p_cal_i))`

**Edge & EV (buying YES at price q, $1 payoff):**
- `edge = p_consensus − q`
- `EV per dollar = p_consensus − q` (after fees: subtract `q · f_eff`)

**Sizing — fractional Kelly (binary contracts):**
- Kelly: `f* = (p − q) / (1 − q)`
- Applied: `min(0.25 · f* · equity, position_cap)`
- Floor: $25.

**Execution thresholds (must all hold):**
- `|edge| ≥ 0.03`
- `|edge| ≥ effective_spread + expected_slippage`
- `consensus_confidence ≥ 0.6`
- `std(p_cal_i) ≤ 0.20` (high disagreement → no trade)
- Not in cooldown (24h after stop-out on same market)

**Multi-leg.** If two markets cover correlated events with implied probabilities inconsistent (e.g., mutually exclusive outcomes summing > 1.05), open a relative-value pair instead of directional.

---

## 6. Risk Management

Limits at nested scopes; trade must pass *every* applicable scope.

**Per-trade:**
- Max notional: 2% of equity
- Min edge: 3% (post slippage + fees)
- Max slippage tolerance: 1% of mid
- Solvency check: rejected if (proposed notional + estimated fees + open-order reservations) > available cash; estimate uses Polymarket-API fee data when available, conservative fallback otherwise.

**Per-market:**
- Max gross exposure (long + short): 4% of equity
- 24h cooldown after stop-out on this market

**Per-category** (politics-elections, crypto-prices, sports-event, macro-economy, science-tech, geopolitics, entertainment):
- Max category exposure: 10% of equity
- Max correlated cluster (auto-detected by 30d covariance): 25%

**Per-agent:**
- Initial: equal weight (1/N)
- Rebalanced weekly by meta-allocator on rolling 30d Sharpe (softmax, T=0.5)
- Performance floor: rolling 30d return < −10% → halve allocation
- Emergency disable: 7d hit rate < 40% (with ≥ 20 resolved samples) OR 7d realized PnL < −8% of agent allocation → suspend, alert

**Portfolio:**
- Max gross exposure: 60% of equity
- Daily VaR(95%): 4% of equity
- Daily realized loss cap: 5% → halt new orders for the day
- Drawdown trip-wires: 5% from peak: warning; 10%: reduce all new sizes by 50%; 15%: kill switch, manual restart.

**Resolution risk (Polymarket-specific):**
- Polymarket resolves via UMA optimistic oracle (~2–7 day delay, dispute possible).
- Markets flagged "ambiguous resolution criteria" by manual review or NLP heuristic: position reduced by 50%.
- No new positions in markets resolving within 1h.
- Force-close at p ≥ 0.99 only when an authoritative external feed confirms outcome and no UMA dispute is open.

---

## 7. Strategy Layer

Strategies are concerns *across* agents — not separate agents. PnL tracked per strategy.

| Strategy | Trigger | Sizing | Notes |
|---|---|---|---|
| **Mispricing (core)** | edge ≥ 3%, high consensus | Kelly | Default; ~70% of capital |
| **Momentum** | 7d trend + supporting news, edge ≥ 1.5% | Half-Kelly | Trail stop at 5% adverse |
| **Contrarian** | Price ≥ 0.95 or ≤ 0.05 with thin news; `base-rate-bayesian` disagrees ≥ 10% | Quarter-Kelly | Tail risk; cap 0.5% per trade |
| **Cross-market arb** | Same event across Polymarket/Kalshi/sportsbook with > 2% gap net of fees | Capped at narrowest depth | v2 — needs multi-venue |
| **News-driven** | NLP flags high-impact news + market hasn't moved 5 min | Quarter-Kelly | 30 min event window |
| **Resolution arb** | Outcome confirmed by external feed, market < 0.97 (or > 0.03) | Up to 5% per market | Requires strong feed certainty |

Weights tuned monthly via attribution. New strategies → 30d shadow before live.

---

## 8. Data Layer

**Sources:**

| Source | Type | Use |
|---|---|---|
| Polymarket CLOB API (WS + REST) | Market | Orderbook, trades, fills |
| Polymarket Gamma API | Market metadata | Resolution criteria, end dates, categories |
| NewsAPI / GDELT | News | Real-time + historical news flow |
| X / Twitter API (filtered) | Social | High-signal accounts, breaking news |
| Reddit (selected subs) | Social | Vertical community sentiment |
| Tavily / Brave Search | Web | Open-ended research queries |
| Kalshi public API | Market | Cross-market reference (read-only v1) |
| FRED, sports stats APIs | Domain | Macro and sports priors |
| Internal: resolved markets archive | Calibration | Agent calibration training |

**Storage:**

| Store | Tech | Data |
|---|---|---|
| Time series | TimescaleDB | `market_snapshots` (1s active, 1m archive) |
| Relational | Postgres 16 | `markets`, `predictions`, `decisions`, `trades`, `positions`, `agent_state`, `cycle_plan`, `operating_doctrine` |
| Hot state | Redis 7 | open positions, current quotes, kill switch flag, rate limits |
| Object | S3-compatible (MinIO local, S3 prod) | raw articles, agent reasoning traces, screenshots |

**Key schemas (sketch):**

```sql
markets(id pk, condition_id, slug, title, category, end_date, status,
        resolution_source, created_at, last_seen, ambiguity_score)

market_snapshots(time, market_id fk, best_bid, best_ask, mid, depth_bid_1pct,
                 depth_ask_1pct, volume_24h)              -- TimescaleDB hypertable

predictions(id pk, time, market_id fk, agent_id, p_raw, p_calibrated, confidence,
            reasoning_uri, research_bundle_id, latency_ms)

decisions(id pk, cycle_id, market_id fk, p_consensus, q_market, edge,
          gate_results jsonb, action, rationale)

trades(id pk, decision_id fk, time, market_id fk, side, size, price, fees,
       status, broker_order_id, parent_trade_id)

positions(market_id fk, side, size, avg_price, unrealized_pnl, realized_pnl,
          opened_at, last_updated)

agent_performance(agent_id, time, hit_rate_30d, sharpe_30d, pnl_30d,
                  isotonic_curve_uri, n_samples)

beliefs(id pk, time, agent_id, domain,
        scope ('global'|'category'|'market'|'position'),
        scope_ref, statement, p_estimate?, confidence, evidence_uri,
        supersedes_id?, status, last_updated)             -- per-agent structured views

notes(id pk, time, agent_id, body, tags jsonb, last_accessed)   -- per-agent scratchpad (LRU)

cycle_plan(id pk, written_at, written_by_cycle_id,
           next_priorities jsonb, holds_with_rationale jsonb,
           pending_settlements jsonb, opportunities_deferred jsonb,
           blockers jsonb, superseded_at?)                -- portfolio-level forward-looking handoff;
                                                         -- exactly one row with superseded_at IS NULL

operating_doctrine(id pk, created_at, target_date,
                   phases jsonb, current_phase, key_risks jsonb,
                   revised_at, revised_by, approved_by,
                   supersedes_id?, status)                -- current operative strategy with lineage;
                                                         -- exactly one row with status='active'
```

**Retention:**
- Snapshots: 90d hot → continuous-aggregate to 1h cold for 5y.
- Trades, decisions, predictions: indefinite.
- Reasoning traces: 1y hot → cold S3.
- `cycle_plan`: latest active row hot; superseded rows kept 90d (replay/audit), then archived to cold S3.
- `operating_doctrine`: full lineage indefinite (operative-state record).

---

## 9. Execution Layer

**Polymarket integration:**
- WebSocket subscribed to orderbooks for tracked markets (open positions + active candidates).
- REST for orders + account state.
- EIP-712 typed-data signing; private key in cloud KMS or hardware key (YubiHSM).
- Network: Polygon mainnet; gas in MATIC. Settlement: USDC.e.

**Order types:**

| Type | Use |
|---|---|
| Limit, post-only | Default; capture maker rebate |
| Limit, marketable | When edge is decaying and speed > price |
| Iceberg slices | Orders > $500 split into 5–10 random-sized chunks with random delay |
| TWAP exit | Position closes for size > 1% of equity |

**Idempotency & reconciliation:**
- Internal idempotency key (tag) on every order.
- On retry, broker queried before resubmit.
- Reconciler every 30s: diffs internal vs broker. Diff > $10 → freeze new orders for that market + page.

**Failure handling:**
- Transient (network, 5xx): exponential backoff (1s, 2s, 4s, 8s; max 60s).
- Circuit breaker: 5 errors / 60s on a service → 5 min cooldown.
- Polymarket outage: halt new orders, monitor only; positions tracked from cached state.
- Anthropic API outage (Claude Opus): no model failover by design. A cycle that cannot reach the API simply fails — the Lead exits, no orders are placed, the next scheduled cycle tries again. If `safety-watchdog` (independent of the team, deterministic, in `risk/`) sees ≥ 3 consecutive cycle-failures, it sets the system to monitor-only mode until recovery; existing positions remain governed by deterministic rules in `risk/` (stop-outs, kill-switch, time-based close), which run independently of the Claude Code process.
- Data source outage: continue with degraded info; flag in decision metadata.

**Slippage control:**
- Pre-trade: simulate fill against orderbook. Reject if expected slippage > `max_slippage` gate.
- Post-trade: realized vs expected logged; sustained excess → tighten estimator.

---

## 10. Evaluation Metrics

**Valuation convention.** Account value mark-to-market against current best **bid** (immediate liquidation value), never mid/ask. Conservative by design. Realized PnL uses fill prices net of fees + gas. Cost basis = weighted-average entry: new entry = (old_qty · old_price + add_qty · add_price) / (old_qty + add_qty).

**Performance (daily/weekly/monthly):**

| Metric | Definition | Target |
|---|---|---|
| Net PnL | Σ realized + Δ unrealized − fees − gas | > 0 monthly |
| Sharpe (annualized) | `mean(daily_ret) / std(daily_ret) · √252` | > 1.5 |
| Sortino | `mean / downside_std · √252` | > 2.0 |
| Max drawdown | peak-to-trough on equity curve | < 15% |
| Calmar | annual return / max drawdown | > 1.0 |
| Hit rate | wins / (wins + losses) | depends on edge dist. |
| Avg win / avg loss | | > 1.2 |

**Per-agent attribution:** Capital ROI; Sharpe contribution; Hit rate; Pairwise correlation (low desired).

**Operational:** Cycle latency (P50/P95/P99); per-stage breakdown; fill rate; slippage realized vs expected (Δ over time); error rates per service.

**Reporting:** Real-time Grafana; daily PnL email; weekly performance + attribution; monthly deep dive + strategy/roster review.

---

## 11. Logging & Observability

**Structured logs (JSON to stdout → Vector/Fluent Bit → Loki).** Required per line: `timestamp, service, level, cycle_id, correlation_id, market_id?, agent_id?, decision_id?, message, ...payload`. Reasoning traces in S3, referenced by URI in `predictions.reasoning_uri`. Every decision reconstructable from logs alone.

**Metrics (Prometheus):**

| Metric | Type | Notes |
|---|---|---|
| `cycle_duration_seconds` | histogram | Per-stage labels |
| `decisions_total` | counter | action, agent_id |
| `trades_total` | counter | side, status |
| `errors_total` | counter | service, type |
| `equity_usd` | gauge | |
| `gross_exposure_usd` | gauge | |
| `drawdown_pct` | gauge | |
| `agent_hit_rate_30d` | gauge | agent_id |
| `kill_switch_active` | gauge | 0/1 |

**Tracing (OpenTelemetry → Tempo/Jaeger):** one trace per decision cycle; spans per service call; sub-spans per agent inference + tool call.

**Dashboards (Grafana):** system health; capital + exposure; per-agent performance; trade flow heatmap.

**Alerts (Alertmanager → PagerDuty/email):**

| Condition | Severity |
|---|---|
| Kill switch activated | info (event) |
| Drawdown > 10% | warn |
| Drawdown > 15% | critical (page) |
| Cycle latency P95 > 30s | warn |
| Reconciliation diff > $10 | critical |
| Error rate > 5% / 5 min | warn |
| Agent disagreement std > 0.30 sustained | info (regime change) |

---

## 12. Safety & Controls

**Kill switch:**
- Global Redis flag `system:kill_switch`.
- HTTP `POST /admin/kill` (auth: signed token + 2FA in prod).
- Order-placing services poll every 1s; on activation halt new orders, optionally close positions.
- Auto-trip: drawdown > 15%; daily loss > 5%; reconciliation diff > $100; error rate > 20% / 5 min.

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

---

## 13. Future Extensions

**RL (v2 — contingent on Anthropic managed fine-tuning for Opus):** if/when exposed, train per-agent variants on resolved-prediction data with PnL-weighted reward. Maintain frozen reference copies for diversity. Shadow-A/B before promotion. Until then, learning loop = prompt optimization (below).

**Meta-learning / prompt optimization:** automated prompt search (DSPy/TextGrad) using historical PnL/hit-rate as objective. Spawn mutated-prompt candidates in shadow paper; promote on 30d outperformance.

**Multi-model cloud ensemble (v2):** add Claude-family models (Sonnet, Haiku) as supplementary backbones — only if performance shows uncorrelated errors with Opus. Cross-vendor cloud only if same test passes *and* audit/data-residency review approves; Claude family preferred. **§4 cloud-only rule is non-negotiable** — no local/on-prem in any v2+. Each new model goes through 30d shadow (§7, §14.10).

**Cross-venue arbitrage:** Kalshi + sportsbook integration; statistical arb engine across venues.

**Real-time event-driven:** sub-second news → trade pipeline (lightweight classifier + dedicated agent); direct microstructure stream for thinner markets.

**Privacy/security maturation:** HSM-backed signing (production hardware); SOC2-style controls if scaling to external capital; ZK proofs for prediction provenance (research).

**Scalar/categorical markets:** beyond binary; new aggregation logic + Kelly variants.

---

## 14. Engineering & AI-Coding Workflow

Sections above describe *what*; this section codifies *how* humans + AI build/modify it.

### 14.1 Repository Layout

```
autonomous_trading/
├── research/        # strategies, backtests, agent prompts, calibration
├── execution/       # order routing, CLOB client, position management
├── risk/            # limits, kill switches, sanity gates, capital gate
├── shared/          # data models, schemas, common utilities
├── infra/           # Docker, k8s, migrations, deployment
├── tests/           # mirrors src layout
├── docs/            # ADRs, runbooks, risk rules
├── .claude/         # project-scoped Claude Code config (committed)
├── CLAUDE.md        # AI-coding guard rails (committed)
├── CLAUDE.local.md  # personal notes (gitignored)
└── specs.md         # this document
```

`risk/` is *protected code* — see §14.7.

### 14.2 CLAUDE.md (project)

Short, high-signal, < 200 lines. Every line must answer *yes* to: "Would Claude make a mistake without this line?" Initial scaffold via `/init`, then manually pruned.

Mandatory:
- Build/test/lint commands (`pytest`, `ruff`, `mypy --strict`)
- No-go list:
  - **NEVER** commit real API keys, private keys, mnemonics, `.env*`
  - **NEVER** trigger live trades without explicit user confirmation in this session
  - **NEVER** modify code under `risk/` outside Plan Mode with explicit approval
  - **NEVER** push directly to `main` or `--force` push
- Repo conventions (commit format, branch naming, PR template ref)
- Pointer to `specs.md` as architectural source of truth
- `@docs/risk-rules.md` import for hard risk rules

Personal/transient → `CLAUDE.local.md` (gitignored). Global → `~/.claude/CLAUDE.md`.

### 14.3 GitHub Integration

- **Claude Code GitHub App** (`/install-github-app`): auto PR reviews, `@claude` mentions, fix pushes.
- **`gh` CLI** required locally — token-cheaper for AI use.
- **`/ultrareview`** before every merge into `main` touching `execution/` or `risk/`.
- **`/security-review`** on every PR touching auth, signing, or secrets.
- **GitHub Actions** with `claude -p` (headless): backtest on PR, AI-code lint, regression detection.
- **Branch protection on `main`**: required PR reviews (≥ 2 for `risk/`), required status checks (`tests`, `backtest`, `security-review`, `mypy-strict`, `gitleaks`), no direct pushes, no force-push.

### 14.4 Hooks (`.claude/settings.json`)

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
| `TaskCreated` (production trading-cycle only) | Lead creates task | Validate task schema matches §3 cycle; reject malformed tasks before members claim |
| `TaskCompleted` (production trading-cycle only) | Member marks task done | Validate output artifact against §14.12 Pydantic schema; exit 2 to force retry on schema mismatch; exit 0 only on clean artifact |
| `Stop` (production trading-cycle only) | Lead about to exit | Assert `clean up the team` was called; if not, force cleanup before allowing exit (Cloud-doc warning: avoid orphaned `~/.claude/teams/` entries) |

### 14.5 Subagents (`.claude/agents/`)

| Agent | Tools | Purpose |
|---|---|---|
| `strategy-researcher` | Read, Grep, WebSearch | Read-only strategy research; produces backtest specs |
| `backtest-runner` | Bash (scoped), Read | Execute backtests, return metrics |
| `risk-reviewer` | Read | Diff `risk/` and trading-decision paths against `docs/risk-rules.md` |
| `security-reviewer` | Read, Bash (scoped grep/gitleaks) | Secrets, injection, signing-path review |

### 14.6 Skills (`.claude/skills/`)

On-demand: `polymarket-api` (endpoints, rate limits, EIP-712 signing, fees); `backtesting-protocol` (required steps + metrics before live); `incident-response` (kill-switch, position-close, key rotation).

### 14.7 Risk Layer Protection

`risk/` contains the *deterministic* guard rails (size caps, daily loss caps, kill-switch triggers, capital gate). AI decides *what* to trade; risk layer decides *whether and how much*.

- All §6 limits = plain constants/config in `risk/`, never AI outputs.
- AI agents may not edit `risk/` outside Plan Mode + explicit user approval (§14.4 hook).
- All trading-decision paths import from `risk/`. Direct CLOB calls bypassing `risk/` forbidden by import-linter in CI.
- `risk/` requires 100% line coverage; CI fails below.

### 14.8 Capital Gate

Hardcoded absolute capital cap, single constant:

```python
# risk/capital_gate.py
MAX_CAPITAL_EUR: Final = <TBD_CAPITAL_CAP_EUR>
```

- `position-manager` rejects any order pushing gross deployed capital above `MAX_CAPITAL_EUR`.
- Constant changed only via: PR with required ≥ 2 reviewer approvals (branch protection), explicit rationale in PR description, audit-log entry on merge.
- Decreases also gated to ≥ 1 reviewer (prevent panic over-reduction).
- Reviewed quarterly.

### 14.9 Backtest-First Gate

No PR modifying `research/` or strategy code merges into `main` without:
- Backtest on latest 90d resolved markets
- Documented metrics in PR: Sharpe, hit rate, drawdown, PnL — vs. previous version
- CI check `backtest-required` passing (branch protection)

Pre-commit hook also runs fast smoke backtest on touched strategies.

### 14.10 Paper-Trading Mandate

Every new/materially-modified strategy → **≥ 30 days** Polymarket paper/sandbox mode (separate test allocation, no real CLOB) before live capital. Expands §7 shadow with concrete promotion gates.

Promotion to live requires:
- Paper Sharpe > 1.0
- Paper max drawdown < 10%
- Paper hit rate ≥ 55% (with ≥ 30 resolved samples)
- `/paper-deploy` run, results posted to PR

### 14.11 Secret Management

- `.env*` in `.gitignore`, blocked by §14.4 hook on write.
- `gitleaks` + `trufflehog` in `.pre-commit-config.yaml`; same on `Stop` hook.
- Private keys never in repo/env: KMS or hardware only.
- API tokens in HashiCorp Vault or AWS Secrets Manager; fetched at startup, never persist to disk.
- Key rotation: `docs/runbooks/key-rotation.md`.

### 14.12 Strict Typing & Property-Based Tests

- `mypy --strict` is hard CI gate.
- All order/position/trade/decision/prediction/`PortfolioState`/`ResearchBundle` objects = Pydantic models. No untyped dicts on those paths.
- Risk-engine functions covered by `hypothesis` property tests, e.g. *"for any (p, q, equity), Kelly size never exceeds the position cap"*.
- Coverage: `risk/` 100%, `execution/` ≥ 90%, rest ≥ 80%.

### 14.13 Custom Slash Commands (`.claude/commands/`)

| Command | Purpose |
|---|---|
| `/backtest <strategy>` | Run backtest, post metrics to current branch/PR |
| `/paper-deploy <strategy>` | Deploy to paper sandbox |
| `/kill-all` | Trigger kill switch (with confirmation; `disable-model-invocation` set so only humans can run it) |
| `/risk-rules` | Print effective limits from `risk/` |
| `/audit <decision_id>` | Replay AI decision: prompt, model, tools, output, market data |

### 14.14 Permission Modes & Sandboxing

**Development sessions** (a human is at the terminal):
- `/permissions` allowlist for safe read-only/local commands (`pytest`, `git diff`, `ruff`, `gh pr view`).
- Auto-mode permitted only for `docs/`, `tests/`, read-only `research/` exploration.
- Auto-mode **forbidden** for any path that can reach live trading endpoints.
- `/sandbox` (OS-level) for any code with network access running unsupervised.
- For development against live-trading endpoints (e.g. running a one-off CLI), interactive permission confirmation is required every session — never allowlisted.

**Production trading-cycle session** (no human at the terminal — one fresh Claude Code Agent Team per cycle, ~12 min lifetime, per §14.22):
- The Lead is launched with `--dangerously-skip-permissions` so it does not block on prompts. This is unavoidable for unattended operation. Blast radius is bounded to the single cycle's lifetime — a misbehaving cycle cannot accumulate damage across cycles, and the next scheduled cycle starts clean.
- Safety in this mode comes from:
  1. **§14.4 hooks** — deterministic guard rails on tool use (block `rm -rf`, block writes to `risk/`, secret scanning, `SessionStart` janitor, `Stop` cleanup-assertion, etc.). Hooks fire regardless of permission mode.
  2. **§6 risk gates + §12 kill-switch** — the only authoritative gate on whether a trade is placed. The team's permission mode is irrelevant here; the risk-engine member can produce a `Decision`, but the deterministic position-manager (`risk/`) and execution-engine still enforce caps and refuse out-of-bounds orders. These services run as separate, long-lived processes — they do **not** die with the cycle.
  3. **§14.22 team hooks** — `TeammateIdle`, `TaskCreated`, `TaskCompleted` validate every artifact before downstream members consume it.
  4. **Network egress allowlist** — the production server can only reach Polymarket CLOB, Anthropic API, configured data sources; everything else is blocked at the firewall, so even a malformed tool call cannot exfiltrate or hit unintended endpoints.
  5. **Filesystem isolation** — production server writes only to repo-local `~/.claude/`, the Postgres/Redis network sockets, and S3 (scoped IAM). No general filesystem access.
  6. **Bounded lifetime** — every cycle process exits after ~12 min by design; a process that fails to exit is killed by the scheduler (e.g. cron + timeout, k8s `activeDeadlineSeconds`).
- The trading-cycle Lead is the **only** environment where `--dangerously-skip-permissions` is acceptable. Every other Claude Code session (dev, improvement-team batches, debug) keeps standard permissions.

### 14.15 Workflow Discipline

- **Plan Mode** (Shift+Tab) for any non-trivial change.
- **Subagents** for investigation; keeps main context clean.
- `/clear` between unrelated tasks.
- `/rewind` / Esc-Esc to undo, never destructive bash recovery.
- **Writer/Reviewer split**: one session writes, fresh session reviews `execution/` + `risk/` changes.
- `/statusline` configured for live token usage.

### 14.16 Parallel Work Setups

- **Claude Code Desktop App** for multiple isolated worktrees in parallel.
- **Claude Code on the Web** for longer autonomous research on cloud VMs.
- **Agent Teams** for orchestrated multi-session workflows on complex refactors.

### 14.17 Audit Trail (development reinforcement of §11)

Every AI decision replayable from logs alone. Required per decision:
- Full input prompt (template + filled context)
- Model id + version + temperature/params
- Raw output (with tool calls + reasoning trace)
- Snapshot of market data used (orderbook, news bundle URI)
- All risk-gate evaluations + results
- Final action taken/rejected, with reason

Stored in `decisions` table (§8); reasoning blob in S3. Replay via `/audit <decision_id>`.

### 14.18 MCP Servers

`claude mcp add` for external systems used during *development*:
- Postgres/TimescaleDB (read-only for research, read-write only for migrations)
- Polymarket API (custom MCP if no public; read-only by default)
- Grafana/Sentry (observability into Claude session)
- Linear or Notion (strategy specs, ticket sync)

MCP servers are **never** wired into live trading-loop services — only developer/Claude sessions.

### 14.19 Development-Side Observability

Reinforces §11:
- Sentry for exceptions in all services.
- Grafana dashboard URL in PR template.
- Alerts on anomalous trade volume routed to operator phone (never to Claude/AI).

### 14.20 Open-Source First Reuse Policy

Before any new component, do a prior-art search; prefer reuse over rewriting.

- Search GitHub, PyPI/crates.io, recent papers (with code). `strategy-researcher` (§14.5) + `prior-art-scout` (§15.1) lead.
- Every PR introducing a non-trivial new component must include a `prior-art` note: what considered, what selected, and — if rewriting — explicit reason.
- Bias: fork + minimal patches > rewrite. Forks declare upstream + sync cadence in `docs/forks.md`.
- Examples to evaluate: Polymarket SDK clients (Python/TS), backtesting frameworks (`vectorbt`, `nautilus_trader`, `backtrader`), agent orchestration (`langgraph`, `dspy`, `pydantic-ai`), calibration (`netcal`, `sklearn.calibration`), order management (`ccxt`).
- License: production may depend only on MIT/BSD/Apache-2/MPL-2. Copyleft (GPL/AGPL) requires legal review + PR sign-off.

**Concrete prior-art adopted.** From a survey of *Prediction Arena* (Arcada Labs, `predictionarena.ai`): per-cycle prompt assembly with recent settlements/recent trades/previous-cycle reasoning/critical-learning section (§4); the **dual knowledge management** pattern from PA's Polymarket implementation — per-agent `manage_notes` scratchpad (~50 × ~200 words, LRU) for ad-hoc memory + structured `manage_beliefs` store typed by domain (`market_structure`/`strategy`/`event`/`risk`/`sentiment`) for first-class market views with revision history (§4 + §2 + §8); bid-based mark-to-market valuation (§10); explicit pre-trade solvency check (§6). Rejected or independently re-derived (stricter): 15% per-market concentration limit (we use 4% in §6), Fill-or-Kill-only order semantics (we use post-only limit + iceberg + TWAP in §9), single-OpenAI-web-search tooling (we keep cloud-only Anthropic per §4 with Tavily/Brave for search), and pooled real-capital structure. The two-tier orchestration view (§2), multi-agent self-improvement loop (§15), multi-tier risk gates (§6), and cloud-only rule (§4) are independently designed.

### 14.21 Centralized Configuration (Single Source of Truth)

**Hard rule.** All numerical thresholds, limits, parameters, and tunables that influence runtime behavior live in **one single settings file** — never duplicated, never hardcoded as magic numbers in service code. The user must be able to change every operational knob from one file.

- Canonical location: `shared/config/settings.py` (Pydantic Settings model) backed by `config/settings.toml` for the values themselves. Single source of truth.
- All services and agents import from this module — no parallel constants, no scattered defaults, no magic numbers in business logic.
- **Examples of values that MUST live there** (non-exhaustive): every limit in §6 (per-trade/market/category/agent/portfolio caps, drawdown trip-wires, daily loss cap), edge / confidence / disagreement thresholds (§5), cycle period and per-stage timeouts (§3), Top-K (§3), iceberg threshold (§9), liquidity filter (§3), agent timeout (§3), reconciler interval / diff threshold (§9), retry/backoff parameters (§9), agent emergency-disable conditions (§6), fee/slippage estimator constants, isotonic refit window size (§5), top-K for belief injection (§4), notes cap (§4).
- **Risk-layer interaction (§14.7, §14.8).** The hardest gates (`MAX_CAPITAL_EUR`, kill-switch triggers, all `risk/`-owned limits) physically live inside `risk/` for protection. The settings file imports and re-exports them — it does **not duplicate** them. Users still see one file with all knobs; `risk/` retains its 2-human-review enforcement on the source.
- **Validation.** Pydantic Settings + `mypy --strict`: missing or wrong-typed values fail at service startup, never silently at runtime.
- **Change governance.** Edits to non-`risk/` settings require ≥ 1 reviewer + CI tests + smoke backtest. Edits to `risk/`-owned values keep the §14.7/§14.8 stricter rules (≥ 2 reviewers, audit-log entry).
- **Hot-reload.** Non-critical operational knobs (cooldown windows, scan filters, dashboard intervals) hot-reload from Postgres-backed config (§12) without restart. Structural values (cycle period, schema fields) are restart-only.
- **Anti-pattern enforcement.** A CI lint rule rejects PRs that introduce a numeric literal in `execution/`, `research/`, or `risk/` outside the settings module (allowlist for trivial constants like `0`, `1`, `2`). Forces every new tunable through the settings file.

This rule is the dual of §14.7 risk-layer protection: §14.7 prevents the AI from changing the *hardest* limits without humans; §14.21 prevents anyone (human or AI) from scattering tunables so widely that no single file shows the full operating envelope.

### 14.22 Agent Teams & Subagents — Production Runtime + Dev Use

**Production trading loop = one fresh Claude Code Agent Team per cycle.** Every cycle (period from central settings, §14.21, default 12 min) the scheduler fires a new `claude` process. The Lead boots the team, runs the §3 decision cycle, calls `clean up the team`, and exits. The next scheduled cycle starts a brand-new process with no in-process state from the previous one. Long-term memory persists in Postgres / S3; short-term memory dies with the process. This makes the §2 *"memory is the only coupling between tasks"* invariant **enforced by construction**, not by trusting a long-lived Lead to reset state correctly.

**Why per-cycle (rationale):**
- Bug isolation: a misbehaving cycle cannot poison the next.
- Memory leaks impossible (process dies).
- Cloud-doc limitations of the experimental feature (no session resumption, fixed Lead, one team per session) become non-issues — every cycle is a fresh session by design.
- Claude Code version upgrades are picked up automatically at the next cycle. If an upgrade breaks the team, exactly one cycle fails and the next one runs the previous (pinned) version after rollback.
- Failure handling is trivial — a failed cycle does not run; the next one tries again. No supervisor with liveness probes, no auto-restart logic.

**Bootstrap (single command per cycle, run by scheduler, not by a human):**

```bash
# .claude/settings.json sets CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 (§14.4).
# Run by cron / k8s CronJob / `/loop` skill.
claude --teammate-mode in-process \
       --dangerously-skip-permissions \
       -p "Bring up the trading-team per .claude/teams/trading-team.spec.json.
           Run one decision cycle per §3 of specs.md.
           On completion, run 'clean up the team' and exit."
```

**Scheduler choice:**
- **Production / paper-mode**: `cron` on the dedicated server, or k8s `CronJob` with `activeDeadlineSeconds` set to (cycle-period + 60s grace). Concurrency policy = `Forbid` so a slow cycle never overlaps with the next start.
- **Local dev / smoke-runs**: the `/loop` skill (`/loop 12m run-trading-cycle`) for hands-on iteration without setting up cron.
- The scheduler is the supervisor. A failed cycle is a non-event — no orders are placed, the next fire-time runs cleanly.

**Cleanup discipline.** The Cloud-doc is explicit: *"Always use the lead to clean up. Teammates should not run cleanup."* Mechanisms to enforce this:
1. The cycle's spawn prompt instructs the Lead to call `clean up the team` before exit.
2. The `Stop` hook (§14.4) asserts cleanup was called; if not, forces it before allowing process exit.
3. The next cycle's `SessionStart` hook (§14.4) sweeps any stale `~/.claude/teams/` directories left by a crashed prior cycle, before spawning the new team.

**Hooks specifically for the team** (in `.claude/settings.json`, see §14.4 table for full set):
- `SessionStart` — janitor + version + spec checks before team spawn.
- `TeammateIdle` — keep members working until artifact written; abort cycle after 3 idle escalations on the same task (single bad cycle is non-fatal, no kill-switch trip).
- `TaskCreated` — schema-validate tasks before claim.
- `TaskCompleted` — Pydantic-validate output artifacts before marking complete.
- `Stop` — assert + force cleanup before exit.

**Subagents** (one-way fan-out, parent-only return — `code.claude.com/docs/en/sub-agents`): used by individual members for focused work where peer dialogue is not needed. Subagent definitions in `.claude/agents/` are reusable as both delegated subagents and team members. Examples per member listed in §2.

**Off-cycle teams.** Same Claude Code feature, separate processes, separate `team-name`:
- **Improvement-agent batches** (§15.1, §15.3) — scheduled separately from the trading cycle; the weekly batch runs its own Improvement Team session, cleans up, exits.
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

**Versioning & rollback.** Claude Code version pinned in `infra/` (e.g. via Docker image digest, not a floating tag). Team-spec source-of-truth (member roster, subagent definitions, system prompts) lives in `.claude/agents/` + `.claude/teams/trading-team.spec.json` in the repo. The runtime `config.json` written by Claude Code is ephemeral per cycle and not edited by hand (Cloud-doc rule). Any change to the team spec is a normal PR through §14.3 branch protection. If a Claude Code release introduces an Agent-Teams-breaking change, exactly one cycle fails before rollback; paper-mode (§14.10) catches this before prod via the upgrade-validation cycles run there.

---

## 15. Self-Improvement & Continuous Learning

Trading agents (§4) make individual market decisions. This **meta-layer** observes outcomes and improves agents/strategies/code over time. Two distinct populations:

1. **Trading agents** (§4) — produce P(YES) in the live cycle.
2. **Improvement agents** (this section) — operate on code, prompts, strategies, configs. Never touch live trading endpoints.

Both run on cloud-hosted Claude Opus per §4 (hard rule). Diversity in role + prompt, not in model. Multi-agent with explicit roles, shared memory, human-in-the-loop checks.

### 15.1 Improvement Agent Roster

| Agent | Role | Trigger | Output |
|---|---|---|---|
| `risk-auditor` | Scan recent trades for risk-rule near-misses, anomalous fills, drawdown precursors | Daily + on alert | `lessons` row |
| `calibration-refiner` | Detect performance drift per trading agent; suggest isotonic recalibration on the latest resolved window | Hourly + on agent suspend | PR updating calibration curve |
| `pattern-miner` | Cluster lessons into recurring patterns | Weekly | `patterns` row + supersedence links |
| `strategy-improver` | Read losing trades + new patterns; propose prompt/strategy/sizing deltas; revise `operating_doctrine` (§4) when phase entry/exit conditions are met or target date is reached | Weekly + after drawdown trip-wire (§6) + on doctrine-phase transition | `proposals` row + draft PR with backtest; on merge of doctrine proposal: new `operating_doctrine` row supersedes prior |
| `prior-art-scout` | Enforce §14.20 — search GitHub/PyPI/papers before custom builds | On every new-component PR open | Prior-art note posted to PR |
| `meta-reviewer` | Aggregate, dedupe, prioritize all proposals; route top-K to operator | Weekly | Decision queue (Slack/email) |

**Team structure.** Improvement agents form their own **Improvement Team** — a *separate* Claude Code Agent Team session, distinct from the Trading Team (§2, §14.22). The "one team per session" Cloud-doc constraint is respected because the Improvement Team runs in **its own Claude Code process**, on schedule, never simultaneously inside the trading-loop session.

- A dedicated **Improvement Team Lead** orchestrates each scheduled batch (hourly, daily, weekly, monthly per §15.3). The Lead session is started by cron / scheduler (`schedule` skill or k8s `CronJob`), runs the batch, and cleans up (`clean up the team` per Cloud-doc) before exiting.
- Members above are spawned per batch and tear down with the team at end of run; their short-term coordination uses the standard task-list + mailbox primitives.
- `pattern-miner` and `strategy-improver` may spawn subagents for fan-out (one subagent per lesson cluster, per category being analyzed).
- The Improvement Team is **isolated from the Trading Team** — separate Lead, separate task list, no shared mailbox. The only coupling is the long-term memory layer: improvement agents *read* episodic + reflective tables and *write* `lessons` / `patterns` / `proposals` / Git PRs.
- Permission mode: standard (no `--dangerously-skip-permissions`). The Improvement Team has no live-trading endpoint access by design (§15.6 safety boundary), so blocking on permission prompts for unexpected tool use is acceptable behavior. If a batch hangs on a prompt, the scheduler kills it after a timeout and pages the operator.

All members are defined as Claude Code subagent definitions (§14.5, `code.claude.com/docs/en/sub-agents`), reusable as both delegated subagents and Improvement-Team teammates (Cloud-doc: *Use subagent definitions for teammates*). Read-only access to live observational data (`predictions`, `decisions`, `trades`, snapshots, calibration history); write access only to `lessons`, `patterns`, `proposals`, and Git via PR on feature branches.

### 15.2 Shared Memory & Notes

Three additive tables (on top of §8):

```sql
lessons(id pk, time, source_agent_id, trigger_event_id, market_id?,
        observation, hypothesis, action_taken, outcome, status,
        parent_pattern_id?, tags jsonb)            -- append-only

patterns(id pk, first_seen, last_seen, occurrences, description,
         supporting_lesson_ids uuid[], confidence)

proposals(id pk, time, source_agent_id,
          target_kind ('prompt' | 'strategy' | 'code' | 'limit' | 'config' | 'operating_doctrine'),
          target_ref, current_value, proposed_value, rationale,
          backtest_result_uri, status, decided_by, decided_at)
```

- `lessons` append-only; supersedence via `status`, never deletion. Replay always possible.
- `patterns` curated by `pattern-miner`; references the lessons that built it.
- `proposals` drives the change pipeline (§15.4).
- A `proposal` with `target_kind='operating_doctrine'` and an accepted `decided_by` write a new `operating_doctrine` row (§4) and supersede the prior active row. The trading layer reads the new doctrine on the next cycle's boot.

Read API exposes these to all improvement agents; write paths scoped by role.

### 15.3 Learning Loop Cadence

- **Hourly**: `calibration-refiner` pulls newly resolved markets, refits per-agent isotonic curves, opens proposals only on material drift (curve KL-divergence vs. previous fit > threshold).
- **Daily**: `risk-auditor` scans last-24h trades; writes lessons for near-misses, outliers, slippage anomalies.
- **Weekly batch** (Mon morning, off-cycle):
  1. `pattern-miner` extracts patterns from week's lessons.
  2. `strategy-improver` generates proposals from new patterns + losing trades.
  3. `meta-reviewer` dedupes, prioritizes, posts top-K queue to operator.
- **Monthly retrospective**: archived lessons (> 90d) compressed into per-category **LTKDs** (Long-Term Knowledge Documents) loaded as background for improvement agents going forward. Stale/contradicted points pruned by `meta-reviewer`.

This compaction is essential — without it the lessons store grows unboundedly.

### 15.4 Checks and Balances

Every improvement-agent change goes through standard merge pipeline. **No improvement agent may self-merge**; none may write to `risk/` (§14.4 hook).

| Target of change | Required reviewers | Required tests/gates |
|---|---|---|
| Prompt (agent template) | 1 agent reviewer + 1 human | Backtest + 30d paper (§14.10) |
| Strategy parameter | 1 agent reviewer + 1 human | Backtest + 30d paper |
| New strategy | `meta-reviewer` + 2 humans | Backtest + 30d paper + risk review |
| `operating_doctrine` revision (§4) | `meta-reviewer` + 1 human | Backtest if quantitative; sanity-check on phase entry/exit conditions |
| Code in `execution/` | `security-reviewer` + 2 humans | Tests + integration tests |
| Risk limit (`risk/`) | **2 humans only** — no agent override | Property-based tests pass |
| Hard cap `MAX_CAPITAL_EUR` | 2 humans + audit-log entry (§14.8) | n/a |

Branch protection on `main` enforces these counts mechanically.

### 15.5 Capital-Allocation Feedback

Meta-allocator (§6) factors:
1. **Trading-performance Sharpe** (existing, primary).
2. **Adaptation quality** — share of agent's lessons that became merged proposals with positive paper outcome (small bonus, capped +10% of base allocation).

Rewards trading agents whose failure modes were genuinely informative — closes loop between trading + improvement layers.

### 15.6 Safety Boundary

Strict separation between improvement layer and live hot path:
- Improvement agents have **no credentials** for production CLOB endpoints.
- Their proposals materialize as Git PRs + `proposals` rows — never direct in-process state changes.
- Read all observational data; write only `lessons`, `patterns`, `proposals`, PR commits on feature branches.
- §12 kill switch + §6 risk limits unaffected by any improvement-agent action.

Result: no runaway self-modification. Human in loop on every merge that affects live behavior.

### 15.7 Failure Modes & Mitigations

| Failure | Mitigation |
|---|---|
| Improvement agent overfits to recent noise | Require ≥ 90d data + significance threshold per proposal |
| `patterns` table bloats with low-value entries | Quarterly `meta-reviewer` prune; confidence-decay on stale patterns |
| Adversarial drift (proposes prompts that game metric) | All metrics validated on held-out forward window before promotion |
| Proposal queue grows into noise | `meta-reviewer` suppresses low-priority; operator sees top-K only |
| Improvement agents converge on bad direction | Mandatory human-in-the-loop; 2-human rule on risk/strategy |
| `lessons` table self-contradicts | `status` field tracks supersession; `meta-reviewer` reconciles in monthly retro |
| Anthropic outage stalls weekly batch | Batch is non-realtime; defer to next cycle, no live impact |
| Improvement agent suggests bypassing `risk/` | §14.4 hook + import-linter prevent the diff existing |

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

## Appendix B — Glossary

- **Edge** — signed difference between consensus probability and market-implied probability of the same outcome, net of expected slippage + fees.
- **Calibration (procedure)** — isotonic recalibration mapping `p_raw → p_cal` from an agent's historical resolved predictions. The procedure is retained even though calibration *quality metrics* (Brier, ECE) are not used in v1.
- **Quarter-Kelly** — position size scaled to 25% of full Kelly; reduces volatility at modest cost in expected log-growth.
- **Resolution risk** — risk that a market resolves contrary to obvious outcome due to ambiguous criteria, oracle dispute, or delay.
- **Effective spread** — bid-ask spread adjusted for fee impact + depth at intended order size.
- **Shadow mode** — strategy/agent running paper-trading on live data, for evaluation before capital allocation.
- **Agent orchestration** — coordination of multiple specialized AI agents into an explicit task pipeline, with typed memory artifacts passed between tasks (Tier 1) and reflective memory accumulated across cycles (Tier 2). See §2.
- **Team Lead Agent** — per-cycle orchestrator (§2). A fresh `claude` process spawned by the scheduler at each cycle: boots the team from the spec, initializes the shared task list, spawns Team Members, routes mailbox traffic, decides Review-gate short-circuit, persists artifacts to long-term stores, calls `clean up the team`, and exits. Lifetime = one cycle (~12 min). Never executes orders directly.
- **Team Member Agent** — independent Claude session with its own context window, owns one Tier-1 stage. Communicates with peers via mailbox + shared task list. Pattern from `code.claude.com/docs/en/agent-teams`.
- **Subagent** — focused worker spawned by a Team Member for one-way fan-out. Result returns to the parent only; no peer messaging, no shared task list. Cheaper than a Team Member because results summarize back into the parent context. Pattern from `code.claude.com/docs/en/sub-agents`.
- **Short-term memory** — per-cycle, ephemeral: shared task list + mailbox + in-flight Pydantic artifacts + per-member context windows. Cleared at cycle close. Never read by future cycles.
- **Long-term memory** — across cycles, durable: `notes`, `beliefs`, `predictions`, `decisions`, `trades`, `positions`, `lessons`, `patterns`, `proposals`, LTKDs. Carries everything future cycles depend on.
- **Working memory** — historical synonym for short-term memory in §2 taxonomy; superseded by the explicit short-term/long-term split.
- **Episodic memory** — per-event durable records (predictions, decisions, trades, positions) used for replay/audit.
- **Reflective memory** — observations + hypotheses written by improvement agents into `lessons` after the fact.
- **Belief** — typed structured statement an agent currently holds about how a market or class behaves. In `beliefs` table; performance-tracked (hit rate) when falsifiable; revisions preserve full lineage. Distinct from `predictions` (per-market per-cycle), `notes` (ad-hoc), `lessons` (post-hoc, by improvement agents).
- **Dual knowledge management** — pattern of giving an agent both unstructured `notes` and structured `beliefs` as separate memory stores, each with its own tool API + lifetime. Adopted from Prediction Arena.
