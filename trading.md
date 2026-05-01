# Trading — The Executing Trading Instance

The runtime that decides what to trade and places orders. One block of the four-block architecture (see `specs.md`).

**What lives here:** the per-cycle Trading Team, the 7-persona agent ensemble, the strategy layer, the decision logic, and the per-trade gates as the trading layer sees them. **What does not live here:** runtime topology (`orchestration.md`), schemas + adapters + observability (`data_infrastructure.md`), risk limit values + safety controls + repo conventions (`engineering.md`). Evaluation and learning loops are in `trading_feedback.md` (Tier 1) and `optimization.md` (Tier 2).

---

## 1. Objective

Deploy capital on Polymarket binary prediction markets to generate risk-adjusted returns by systematically identifying and exploiting probabilistic mispricings. A heterogeneous ensemble of AI agents produces probability estimates compared against market-implied probabilities to extract expected-value edge.

**Core pattern.** This is an **agent-orchestration system**, not a single LLM with plumbing. Specialized agents perform distinct tasks (research, prediction, aggregation, risk gating, execution), pass typed memory artifacts within a cycle, and accumulate cross-cycle memory (see `trading_feedback.md` and `optimization.md`). Decisions emerge from the pipeline; outcomes feed back into memory.

**Primary KPIs:**
- Net Sharpe > 1.5 (annualized, post fees + gas)
- Max drawdown < 25% of equity
- Net PnL > 0 monthly; positive trailing 90d return
- Edge consistency: no single category > 50% of PnL

**Out of scope (v1):** non-binary scalar markets, leverage beyond bankroll, market-making, externally-pooled capital.

---

## 2. Agent-Aufbau (the 7-persona ensemble)

**Common contract.** Every agent implements:

```python
class Agent:
    id: str
    version: str
    prompt_template: str            # versioned, in repo
    tools: list[Tool]               # explicit allow-list

    def predict(self, market: Market) -> Prediction:
        """Calls research tools (web_search, news_fetch, analogue_lookup,
        related_market_scan) inline as needed during inference.
        Returns Prediction(p_yes, reasoning_trace_uri, sub_questions, key_evidence)."""
```

**Model.** All agents run on **Anthropic Claude Opus exclusively** (latest, pinned per release). Diversity comes from prompt × tool × persona, not from model heterogeneity. Trade-off: simpler ops/pinning/audit; no cross-provider drift; an Anthropic outage is a system-wide constraint (see `data_infrastructure.md §1`).

**Hard rule — cloud inference only (binding for all current and future versions).** Inference exclusively against managed cloud LLM endpoints. **No self-hosted, on-prem, locally-run, or edge-deployed inference** — not in v1, v2+, sub-component, research, shadow, or paper mode. Rationale: deterministic model-version provenance for replay, capability/safety tracking, vendor-managed eval pipelines, ops simplicity, no self-managed weight integrity risk. Future model additions (see `optimization.md §9`) must be cloud-hosted.

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

**External validation of the 7-role pattern.** TradingAgents (Tauric Research, arXiv:2412.20138) experimentally validates a 7-role financial-LLM ensemble (Fundamentals/Sentiment/News/Technical Analyst, Researcher, Trader, Risk Manager) with measurable gains in cumulative return, Sharpe, and max drawdown over baselines. We adapt the role taxonomy to prediction-market specifics; the structural pattern of seven specialized roles is confirmed prior art.

**Per-agent state:** predictions log (joined to outcome on resolution); capital allocation (`meta-allocator`, see `orchestration.md §3`); internal portfolio (subset of global); performance history (rolling 30d hit rate, PnL — see `trading_feedback.md §5`).

**Independence guarantees:**
- Agents do not see each other's outputs before producing their own.
- Separate processes, independent LLM calls.
- Aggregation by a separate, deterministic service.

**Per-cycle prompt context** (assembled deterministically, on top of system prompt with role/philosophy/risk/tools/protocol). Most fields are sourced from the upstream `PortfolioState` artifact built in §5 step 2 (Review) — the prompt does not re-query state, it consumes the already-snapshotted view, so all agents in a cycle see consistent numbers:
- Current timestamp + cycle metadata
- Market data: orderbook snapshot, settlement criteria, depth at ±1% of mid
- **Account state** (from `PortfolioState`): cash, positions, unrealized + realized PnL, gross exposure, remaining capacity under per-trade gates (see `engineering.md §1`)
- **Recent settlements** (from `PortfolioState`) — last 10 resolved markets with realized PnL
- **Recent closed trades** (from `PortfolioState`) — last 10 trades with realized PnL
- **Previous-cycle reasoning** — agent's own prior reasoning on same market/category
- **Previous cycle's `cycle_plan`** — operational handoff: priorities, holds-with-rationale, identified-but-deferred opportunities, blockers (e.g. settlement bottleneck). Read-only context for the agent.
- **Active `operating_doctrine`** — current phase, phase-specific actions, target date, key risks. Directive: agent's actions should be coherent with the current phase.
- **Critical-learning section** — curated `lessons`/`patterns` excerpts (see `optimization.md §2`): losing patterns to avoid, winning patterns to replicate, position-management reminders. Curated weekly by `meta-reviewer`; injected per cycle by the Team Lead at boot.
- The agent's most recent `notes`
- The agent's active `beliefs` for this market / category / open positions (top-K by relevance + recency)
- Step-by-step trading protocol

This is the explicit bridge between Tier-1 working memory and Tier-2 reflective memory. Every decision is conditioned on a defined, audit-inspectable slice of past experience.

**Agent notes (per-agent scratchpad).** Private `notes` store via `manage_notes` tool (read/write/search/edit). Bounded: max 50 × ~200 words, LRU. Used by the agent itself for patterns spotted during reasoning, reminders for next cycle, tentative hypotheses too provisional for `lessons`. Private to one agent (no cross-agent leakage during prediction — preserves §2 independence) but readable by Code-Evaluation agents (see `optimization.md §2`) for pattern mining. **Schema:** `data_infrastructure.md §1`.

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

Schema: `data_infrastructure.md §1`.

**Position-thesis beliefs (special case).** When a trading agent's prediction directly drives a position open, the execution-engine writes a `scope=position` belief alongside the `decisions` row, copying the agent's reasoning summary as the `statement` and the entry edge as `p_estimate`. On every subsequent cycle while the position is open, the per-cycle prompt for the originating agent injects this belief automatically; if the market price has moved > X% (default 15%, central settings — see `engineering.md`) against the entry, the belief is auto-flagged for `revise` or `retire`. This closes the loop "why am I still holding this?" — every open position has a tracked thesis with full revision lineage.

**Beliefs vs notes vs lessons vs predictions vs cycle_plan vs operating_doctrine:**

| Layer | Granularity | Author | Lifetime | Use |
|---|---|---|---|---|
| `predictions` | per-market, per-cycle | trading agent | event-bound | Direct probability now; consumed by aggregator |
| `notes` | free-form, per-agent | trading agent | LRU-capped | Ad-hoc scratchpad, no schema |
| `beliefs` | typed, structured, per-agent | trading agent | revisable, history kept | First-class views the agent operates *under* |
| `cycle_plan` | portfolio-level, single active row | Team Lead at cycle close | overwritten next cycle, history kept | Forward-looking handoff between cycles |
| `operating_doctrine` | portfolio-level, single active row with lineage | `strategy-optimizer` (proposed) + human (approved) | revisable, lineage kept | Currently in-force operative strategy |
| `lessons` | post-hoc observation | Trade-Eval or Code-Eval agent | append-only | Reflective, written *about* the system |

**Belief usage:**
- Injected per cycle as *"your active beliefs about this market/category"* — top-K by relevance + recency.
- Falsifiable beliefs (with `p_estimate`) are performance-tracked: hit rate on belief outcomes alongside per-cycle predictions (see `trading_feedback.md §5`).
- Settlement/move that contradicts an active belief auto-flags it next cycle; agent decides `revise` (new belief with `supersedes_id`) or `retire`.
- Code-Evaluation agents (see `optimization.md §2`) mine beliefs across the agent population for convergent/divergent views, surface stale beliefs, lift recurring true beliefs into shared `patterns`.

This makes the agent's *implicit world model* explicit, inspectable, and (for falsifiable beliefs with `p_estimate`) hit-rate-trackable.

**Cycle plan (forward-looking handoff).** Single-row portfolio-level artifact written at the very end of each cycle by the Team Lead, read by the *next* cycle's Lead at boot. Solves the gap that fresh-team-per-cycle creates: the next process knows nothing about what the previous one was about to do. Distinct from `notes` (per-agent, retrospective) and `lessons` (reflective, written by Code-Evaluation agents). Fields:
- `written_at` / `written_by_cycle_id`
- `next_priorities` — ordered list of concrete actions for the next cycle
- `holds_with_rationale` — for each currently-open position: 1-line reason to hold rather than close
- `pending_settlements` — markets awaiting resolution that will free capital / trigger PnL recognition
- `opportunities_deferred` — opportunities identified this cycle but not actionable (e.g. capital locked) with the trigger condition under which to revisit
- `blockers` — operational constraints (e.g. capital lock from large open position, data-source outage)
- `superseded_at` — set when the next cycle writes its own plan (history retained for audit/replay)

The Lead synthesizes the plan from in-flight artifacts at step 9 of §5; it is not produced by an LLM call, it's a deterministic summarization of the cycle's `Decision`/`PortfolioState`/aggregator outputs (with one short LLM-written rationale field per priority/hold). Always exactly one active row.

**Operating doctrine (current operative strategy).** Single active row, revisable with full lineage (analogous to `beliefs`). Distinct from LTKDs (see `optimization.md §5`, retrospective background context for Code-Evaluation agents) and `proposals` (change requests, see `optimization.md §2`). This is the **currently in-force operative strategy** that trading agents condition on. Fields:
- `target_date` — horizon the doctrine is written against
- `phases` — ordered list of phases, each with `name`, `entry_condition`, `exit_condition`, `actions`, `forbidden`
- `current_phase` — the phase active right now (re-evaluated each cycle by the Lead against the `entry_condition`/`exit_condition` of adjacent phases; transitions are logged to `decisions`-style audit)
- `key_risks` — risks the doctrine explicitly anticipates
- `revised_at` / `revised_by` — author and timestamp; lineage via `supersedes_id`
- `approved_by` — human approver (mandatory; revisions follow `optimization.md §7` review counts)

Revision authored exclusively by `strategy-optimizer` (see `optimization.md §2`, exploit track) as a `proposal`; merging a doctrine `proposal` writes a new active `operating_doctrine` row and supersedes the prior. Trading agents and the Lead read but never write. Schemas in `data_infrastructure.md §1`.

---

## 3. Strategie / Strategy Layer

Strategies are concerns *across* agents — not separate agents. PnL tracked per strategy. **v1 runs exactly one strategy: Mispricing.** Additional strategies are out of scope for v1; the explore track (`optimization.md §2` `strategy-explorer`) may propose new ones over time, subject to §3.1 and the review counts in `optimization.md §7`.

| Strategy | Trigger | Sizing | Notes |
|---|---|---|---|
| **Mispricing (core)** | edge ≥ 3%, ensemble agrees | Model-proposed, clipped by per-trade gates | The only active strategy in v1; receives 100% of allocated capital |

Sizing is the model's call, clipped by per-trade gates (see `engineering.md §1`).

### 3.1 Anti-whipsaw rule

A live strategy must run **≥ 5–7 days in `real_capital` mode** (default 5, central settings) before `strategy-explorer` (see `optimization.md §2`) may propose a *replacement* strategy. **Refinements via `strategy-optimizer`** (parameter tweaks, prompt rewrites of existing agents, sizing adjustments) are NOT blocked — only full strategy displacement is. Distinct from the paper-mode minimum (≥ 30 days, see `trading_feedback.md §7`). The rule prevents whipsawing between whole strategies before any has accumulated enough live data to be evaluated.

New strategies enter via `optimization.md §3` (explore track) → ≥ 30 days in paper mode → flip to real_capital → ≥ 5–7 days live before next replacement is eligible.

---

## 4. Daten-Inputs für den Cycle

The trading agents consume a specific slice of data per cycle, all sourced through the infrastructure layer. Full source list, schemas, and ingestion mechanics in `data_infrastructure.md §1`.

What an agent receives per cycle:
- **Universe snapshot** — all currently-tradable Polymarket binary markets (orderbook, bid/ask, settlement rules). No filter applied: agent triages itself during inference.
- **Portfolio snapshot** — `PortfolioState` artifact built by `portfolio-reviewer` at step 2 of §5.
- **Memory** — agent's own `notes` + `beliefs` (top-K relevant), previous cycle's `cycle_plan`, active `operating_doctrine`, curated `lessons`/`patterns` excerpts.
- **Research tools (called inline during inference)** — `web_search`, `news_fetch`, `historical_analogue_lookup`, `related_market_scan`. The agent decides per market whether to invoke.

Agents do not directly read `market_snapshots` time-series, raw news feeds, or position-manager state — those flow through the infrastructure-managed snapshot artifacts.

---

## 5. Trading Loop

**Cycle period:** 12 min (configurable, central settings). Multiple time scales (managed by infrastructure, see `orchestration.md §1`):

| Loop | Period | Function |
|---|---|---|
| Snapshot | 1 s | Update orderbook for active positions |
| Reconcile | 30 s | Match internal positions to broker truth |
| Decision | 12 min | Full scan → decide → execute (Trading Team, this section) |
| Resolution | 1 min | Detect resolved markets, finalize outcome + PnL + per-agent hit-rate (`trading_feedback.md §2`) |
| Allocation | 24 h | Rebalance per-agent capital (`meta-allocator`, deterministic, not a Claude team) |
| Code-Eval batches | hourly / daily / weekly | Read trade outputs, propose code/prompt/strategy/agent changes as Git PRs (`optimization.md §2`) |

**Decision cycle (T = scheduler fire time, i.e. a fresh `claude` process started).** The four-stage spine is **Receive → Review → Analyze → Decide** (Prediction-Arena pattern); a Boot phase runs first, and Cycle-close tears the team down. Steps below are the concrete sub-stages.

0. **T+0–15s — Boot.** Scheduler starts a fresh Claude Code process with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` and `--dangerously-skip-permissions`. A janitor first sweeps stale `~/.claude/teams/{team-name}/` directories left by a previous cycle that crashed without cleanup. Lead loads CLAUDE.md, MCP servers, hooks, the team-spec from `.claude/teams/trading-team.spec.json` (see `engineering.md`), then spawns members per the spec — each loads its subagent definitions and waits idle for tasks. Long-term memory pointers (current `MAX_CAPITAL_EUR`, current `TRADING_MODE`, recent settlement IDs) are read from Postgres and exposed to the Lead. The Lead also reads the **previous cycle's `cycle_plan`** (forward-looking handoff: priorities, holds, blockers) and the **active `operating_doctrine`** row (current phase + target date) from Postgres — both are propagated to members via the §2 prompt context. Boot completes when every member reports idle.
1. **T+15s — Receive: Universe snapshot.** All currently-tradable Polymarket binary markets are pulled — orderbook prices, bid/ask spreads, settlement rules. **No filter applied** (PA-style): the agent ensemble sees the full universe and triages itself during inference (step 3). The only exclusion is markets with structurally undefined data (no orderbook, no settlement criteria parseable).
2. **T+25s — Review: Portfolio.** `portfolio-reviewer` builds `PortfolioState`: cash balance, current positions, unrealized + realized PnL, **last 10 settlements with realized PnL**, **last 10 closed trades with realized PnL**. Mark-to-market against current best **bid** (PA convention, `trading_feedback.md §5`). The artifact is consumed by the risk-engine (for solvency + per-cycle-cap checks) and injected into agent prompts (§2).
3. **T+45s – T+5m — Analyze: Agent inference (with model-driven research).** Each agent gets the full universe + `PortfolioState` + memory context (§2) and runs inference. Research is **agent-driven**, not pre-dispatched (PA-style): each agent has tool access to `web_search`, `news_fetch`, `historical_analogue_lookup`, and `related_market_scan`, and calls them on the markets it chooses to focus on. The Member's own reasoning loop decides which markets are worth scoring and which to pass on. Output per scored market: `P(YES)`, reasoning trace URI, list of research-tool calls made (persisted to S3 for audit). Parallel across agents; per-agent timeout 90s. Failed agents excluded from this market only.
4. **T+5m — Analyze: Aggregation.** Simple mean of `p_raw_i` across active agents per market: `p_consensus = mean(p_raw_i)`. Disagreement metric retained as `std(p_raw_i)` for visibility, but no calibration step.
5. **T+6m — Analyze: Edge computation.** `q = best_ask` (for buying YES) or `1 − best_bid` (for buying NO). `edge = p_consensus − q`. Trade only if `|edge| ≥ 0.03` (central setting — see `engineering.md`).
6. **T+7m — Decide: Risk gates** (PA-style, three checks in fixed order, full enforcement in `engineering.md §1`):
   1. **Concentration** — proposed notional ≤ 15% of equity in any single market.
   2. **Solvency** — cash ≥ proposed notional + estimated fees + open-order reservations.
   3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap (central setting).
   Reject any trade that fails any gate. Plus the constitutional `MAX_CAPITAL_EUR` hard cap (see `engineering.md`).
7. **T+8m — Decide: Sizing.** Agent proposes notional. Risk-engine clips to the smaller of: concentration cap remaining, solvency headroom, per-cycle-cap remaining. No Kelly formula — sizing is the model's call within hard limits.
8. **T+9m — Decide: Order placement.** Orders execute **immediately** (PA-style) as marketable limit orders at the current best ask (buy) or best bid (sell). No post-only default, no iceberg slicing, no TWAP. Internal idempotency key on every order. In paper mode (see `engineering.md`) the order is written to the paper-trading ledger instead.
9. **T+11m45s — Persist & emit.** Lead persists in-flight artifacts (decisions, fills, reasoning links) to long-term stores (`data_infrastructure.md §1`); writes any agent-initiated `notes` / `beliefs` / `lessons` updates; **writes a fresh `cycle_plan` row** (forward-looking handoff for the next cycle: top priorities, holds-with-rationale, pending settlements, identified-but-deferred opportunities, blockers) — synthesized by the Lead from the cycle's artifacts; emits cycle metrics. `operating_doctrine` is read-only here (only `strategy-optimizer` revises it, see `optimization.md §2`).
10. **T+11m55s — Cycle close.** Lead calls `clean up the team` (Cloud-doc warning: never let a member run cleanup), shutting down all members and the team config. Lead process exits. The next cycle is a brand-new `claude` process started by the scheduler at the next fire time — no shared in-process state with this cycle. All short-term memory (mailbox, task list, member context windows) dies with the process; only what was persisted in step 9 survives.

---

## 6. Decision Logic

PA-aligned, deliberately lean. No calibration layer, no Kelly sizing, no multi-leg construction in v1.

**Per agent:** raw output `p_raw ∈ (0, 1)`. No isotonic recalibration, no per-agent confidence score.

**Aggregation:** simple mean across active agents.
- `p_consensus = mean(p_raw_i)`
- `disagreement = std(p_raw_i)` — retained for visibility / logging, not used as a gate.

**Edge & EV (buying YES at ask `q`, $1 payoff):**
- `edge = p_consensus − q`
- `EV per dollar = p_consensus − q` (subtract `q · f_eff` for fees if material)

**Trade trigger (must hold):**
- `|edge| ≥ 0.03` (central setting)

**Sizing.** Agent proposes notional. Risk-engine clips to the smallest of: concentration cap (15% of equity), solvency headroom, per-cycle-cap remaining (see `engineering.md §1`). No Kelly formula — the model owns sizing within hard limits.

---

## 7. Per-Trade Gates (consumer view)

The trading layer **consumes** the gates defined in `engineering.md §1`. Three gates, applied in fixed order before any order submission:

1. **Concentration** — proposed notional ≤ 15% of equity in any single market.
2. **Solvency** — cash ≥ proposed notional + estimated fees + open-order reservations.
3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap.

Plus the constitutional `MAX_CAPITAL_EUR` hard cap. Limits are plain constants in `risk/`, never AI outputs. The trading layer is forbidden from defining its own limits or bypassing the gate path — enforced by import-linter in CI (see `engineering.md`).

---

## 8. Strategy Skill Library

Pattern adapted from Voyager (Wang et al., NeurIPS 2024): an open-ended agent maintains a growing library of executable skills as named, reusable code rather than re-deriving the same logic each invocation.

**For us, a "skill" is a deterministic helper for a recurring trading sub-task** that has been validated through paper mode and operator review. Examples a skill might embody:
- `compute_implied_distribution(market_set)` — derive a price-implied distribution across a related set of binary markets (e.g. tiered BTC-price markets).
- `detect_news_freshness(market_id, window_min)` — score how stale current market price is relative to news flow, useful when the mispricing thesis depends on a news event the market may not have absorbed.
- `find_correlated_basket(market_id)` — identify the cluster of related markets for diversification / arb checks.

**Promotion path** (from observation to skill):
1. A `pattern` (see `optimization.md §2`) recurs across ≥ N lessons with a clear procedural description.
2. `strategy-optimizer` proposes lifting it to a skill: writes the executable, a unit-test, a manifest entry. Goes through normal PR review.
3. After merge, the skill lives in `research/skills/<id>.py` with manifest in `research/skills/index.toml` (id, version, owner, dependencies, paper-validation reference).
4. Trading agents may invoke the skill **by name** through a `call_skill(name, args)` tool added to their allow-list. Read-only consumers; agents cannot create or edit skills at runtime.

**Invariants:**
- Skills are deterministic Python functions (no LLM calls, no I/O beyond passed-in data) so they replay identically across cycles.
- Versioned in `index.toml`; an agent's prompt context lists available skill names and signatures (not full bodies).
- `call_skill` results count as research outputs in the audit trail (logged to S3 alongside reasoning).
- `optimization.md §2` `strategy-optimizer` is the only writer; `optimization.md §7` review counts apply (parameter-tweak class).

This gives a concrete inspectable artifact between abstract `lessons`/`patterns` and committed strategy code, and lets the system accumulate procedural knowledge without hardcoding it into individual agent prompts.

---

## See also

- `orchestration.md` — runtime topology, team mechanics, capital allocation.
- `data_infrastructure.md` — schemas, prediction-market adapter, execution engine, observability.
- `engineering.md` — risk-limit values, safety controls, central settings, repo conventions, hooks.
- `trading_feedback.md` — Tier 1 evaluation, results, learnings, paper-mode promotion, the Strategy Update Loop that flows back into agent prompts.
- `optimization.md` — Tier 2 code evaluation, exploit/explore tracks, anti-whipsaw rule (§3.1 above is the trading-side reference), the Tuning Loop that produces the Git PRs the operator merges.
- `specs.md` — architecture diagram and entry point.
