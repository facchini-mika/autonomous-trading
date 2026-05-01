# Infrastructure — Platform, Data, Execution, Ops

The platform layer that the other three blocks depend on. Owns: prediction-market interface (Polymarket primary, Kalshi via shared adapter), data ingestion, storage, order execution against the CLOB, position tracking, observability, safety controls, central settings, and all engineering / AI-coding governance.

**What lives here:** all schemas (long-term memory tables), all hard-numeric limits, the prediction-market adapter, execution mechanics, observability stack, central settings, repository conventions, hooks, secret management. **What does not live here:** trading decision logic (`trading.md`), evaluation metrics interpretation (`trading_feedback.md`), code-change governance specifics (`optimization.md §7`).

---

## 1. System Architecture

**Three independent teams**, each running as its own Claude Code Agent Team in its own scheduled `claude` process. They never share a session, only the long-term memory layer (§3). Decoupled scheduling is the point — a stuck team cannot block the others.

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
[execution-engine]          → Trades + fills (paper or real-capital, §8.9)
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

**Agent-Team topology (Lead + Members + Subagents).** The trading loop is implemented as **one Claude Code Agent Team per cycle** (`code.claude.com/docs/en/agent-teams`) — the experimental feature is the production runtime. Every cycle, a scheduler fires a fresh `claude` process; the Lead boots the team, runs one cycle, cleans up, exits. Long-term memory persists across cycles in Postgres / S3 (§3); short-term memory (mailbox, task list, member context windows) lives only inside one cycle's process and dies with it.

**Why fresh-team-per-cycle (vs. one persistent team running 24/7):** the design rule *"Memory is the only coupling between tasks"* is **enforced by construction** rather than by trusting a long-lived Lead to reset member contexts correctly. A bad cycle cannot poison the next; memory leaks are physically impossible; Claude Code version upgrades pick up at the next cycle naturally; the Cloud-doc limitations (no session resumption, fixed Lead, one team per session) all become non-issues because every cycle starts a fresh session anyway. Cost: a one-off boot of ~5–30s per cycle, which is < 5% of a 12-minute cycle period and happens entirely before edge-time-sensitive work begins.

**Operational pre-conditions** (binding):
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` set in `.claude/settings.json` (§8.4).
- Claude Code v2.1.32 or later, pinned in `infra/` (§8.22).
- Each cycle's Lead process is started with `--dangerously-skip-permissions` so it does not block on interactive permission prompts during unattended operation. Safety in this mode comes **exclusively** from §2 risk-gates, §6 kill-switch, and §8.4 hooks — the permission system is no longer a defense layer (see §8.14). Blast radius of any one cycle is bounded by these gates.
- The cycle scheduler (cron / k8s `CronJob` / `/loop` skill) fires every cycle period (12 min, set in central settings §8.21). A missed or failed cycle is a non-event: no team is started, no orders are placed, the next scheduled cycle simply runs. There is no supervisor with liveness probes, no auto-restart logic — the cron is the supervisor.
- The Lead **must** call `clean up the team` before process exit (Cloud-doc warning). Any cycle process that crashes without cleanup leaves a stale `~/.claude/teams/{team-name}/` directory; a janitor step at the start of the next cycle (§8.22) sweeps stale entries before spawning the new team.
- Team config (`~/.claude/teams/{team-name}/config.json`) is runtime state, written by Claude Code, never manually edited (Cloud-doc warning). The team-spec source-of-truth lives in `.claude/teams/trading-team.spec.json` in the repo and is read at boot time.

**Topology:**

```
┌────────────────── Team Lead Agent (per cycle) ──────────────────┐
│  - drives cycle clock; initializes shared task list              │
│  - spawns members at the right time, passes typed artifacts      │
│  - consumes the §2 risk gates' results to short-circuit if blocked│
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
| **Short-term hand-off** | **In-flight typed artifacts** in process memory: `PortfolioState`, `Prediction`, `ConsensusProbability`, `Decision` | one cycle (persisted on close to §3 for replay only) | Downstream members of same cycle |
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
- Every artifact has a Pydantic schema (§8.12). No untyped dicts cross task boundaries.

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

## 2. Risk Management

PA-aligned: three deterministic gates plus the constitutional capital cap. All limits are plain constants in `risk/` (§8.7), never AI outputs.

**Per-trade gates** (applied in order; trade rejected on first failure):

1. **Concentration** — proposed notional ≤ **15% of equity** in any single market (PA standard).
2. **Solvency** — cash ≥ proposed notional + estimated fees + open-order reservations. Estimate uses Polymarket-API fee data when available, conservative fallback otherwise.
3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap (default 25% of equity, central setting §8.21).

**Constitutional cap (§8.8):**
- `MAX_CAPITAL_EUR` is a hard, two-human-approval-only ceiling on gross deployed capital. Independent of all other gates.

**Per-agent allocation:**
- Initial: equal weight (1/N).
- Rebalanced weekly by `meta-allocator` on rolling 30d hit rate + PnL (softmax, T=0.5).
- An agent that the operator manually flags as broken can be disabled; no automatic suspend rule.

**Manual kill-switch (§6).**
- Operator can halt all new orders via the kill-switch flag in Redis. Existing positions remain open; the safety-watchdog respects the flag at the gate. There are no automatic drawdown trip-wires — drawdown is monitored (`trading_feedback.md §5`) and surfaced via alerts (§5), but action is the operator's call.

**Resolution risk (Polymarket-specific, deterministic):**
- Polymarket resolves via UMA optimistic oracle (~2–7 day delay, dispute possible). Surfaced in agent prompts as market metadata; no automatic position reduction.

---

## 3. Data Layer

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
| Internal: resolved markets archive | Performance tracking | Per-agent hit-rate / PnL evaluation |

**Storage:**

| Store | Tech | Data |
|---|---|---|
| Time series | TimescaleDB | `market_snapshots` (1s active, 1m archive) |
| Relational | Postgres 16 | `markets`, `predictions`, `decisions`, `trades`, `positions`, `agent_state`, `cycle_plan`, `operating_doctrine`, `lessons`, `patterns`, `proposals` |
| Hot state | Redis 7 | open positions, current quotes, kill switch flag, rate limits |
| Object | S3-compatible (MinIO local, S3 prod) | raw articles, agent reasoning traces, screenshots |

**Key schemas (sketch):**

```sql
markets(id pk, condition_id, slug, title, category, end_date, status,
        resolution_source, created_at, last_seen, ambiguity_score)

market_snapshots(time, market_id fk, best_bid, best_ask, mid, depth_bid_1pct,
                 depth_ask_1pct, volume_24h)              -- TimescaleDB hypertable

predictions(id pk, time, market_id fk, agent_id, p_raw,
            reasoning_uri, research_bundle_id, latency_ms,
            outcome?, pnl_realized?)                       -- outcome/pnl set by Trade Eval Team

decisions(id pk, cycle_id, market_id fk, p_consensus, q_market, edge,
          gate_results jsonb, action, rationale)

trades(id pk, decision_id fk, time, market_id fk, side, size, price, fees,
       status, broker_order_id, parent_trade_id,
       realized_pnl?)                                      -- realized_pnl set on settlement

positions(market_id fk, side, size, avg_price, unrealized_pnl, realized_pnl,
          opened_at, last_updated, status)

agent_performance(agent_id, time, hit_rate_30d, sharpe_30d, pnl_30d, n_samples)

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

-- Reflective tables (written by evaluation tiers, see optimization.md §2 + trading_feedback.md §4):

lessons(id pk, time, source_agent_id, trigger_event_id, market_id?,
        observation, hypothesis, action_taken, outcome, status,
        parent_pattern_id?, tags jsonb)            -- append-only

patterns(id pk, first_seen, last_seen, occurrences, description,
         supporting_lesson_ids uuid[], confidence)

proposals(id pk, time, source_agent_id,
          target_kind ('prompt' | 'strategy' | 'code' | 'limit' | 'config'
                       | 'operating_doctrine' | 'agent_roster'),
          target_ref, current_value, proposed_value, rationale,
          paper_validation_uri, pr_url?, status, decided_by, decided_at)
```

**Retention:**
- Snapshots: 90d hot → continuous-aggregate to 1h cold for 5y.
- Trades, decisions, predictions: indefinite.
- Reasoning traces: 1y hot → cold S3.
- `cycle_plan`: latest active row hot; superseded rows kept 90d (replay/audit), then archived to cold S3.
- `operating_doctrine`: full lineage indefinite (operative-state record).

---

## 4. Prediction-Market Interface (adapter abstraction)

The trading and evaluation layers must be venue-agnostic where the abstraction is feasible. **Polymarket is the primary venue (v1)**; Kalshi is a secondary read-only reference. The interface below makes adding a venue an additive change, not a rewrite.

```python
class PredictionMarketAdapter(Protocol):
    """Venue-agnostic interface; concrete impls per venue."""

    # Universe + market data
    def get_universe(self) -> list[Market]: ...
    def get_orderbook(self, market_id: str) -> Orderbook: ...
    def get_market_metadata(self, market_id: str) -> MarketMetadata: ...

    # Resolution + outcomes
    def get_resolved_markets(self, since: datetime) -> list[Resolution]: ...
    def get_resolution(self, market_id: str) -> Resolution | None: ...

    # Account state (write capability gated by mode and credentials)
    def get_positions(self) -> list[Position]: ...
    def get_cash(self) -> CashBalance: ...

    # Order execution (gated; the read-only adapter raises on call)
    def place_order(self, order: Order) -> OrderResult: ...
    def cancel_order(self, order_id: str) -> CancelResult: ...
    def get_order_status(self, order_id: str) -> OrderStatus: ...
```

**Implementations:**

- **`PolymarketAdapter`** — primary. WebSocket subscribed to orderbooks for tracked markets (open positions + active candidates). REST for orders + account state. EIP-712 typed-data signing; private key in cloud KMS or hardware key (YubiHSM). Network: Polygon mainnet; gas in MATIC. Settlement: USDC.e. Polymarket Gamma API for resolution lookup. **Full read-and-write** — used by Trading Team in `real_capital` mode.
- **`KalshiAdapter`** — secondary, read-only v1. `get_universe`, `get_orderbook`, `get_market_metadata`, `get_resolved_markets` only; `place_order` raises `NotImplementedError`. Reserved for cross-venue price comparison if/when the explore track promotes a strategy that needs it.
- **`PaperTradingAdapter`** — wraps `PolymarketAdapter` for read paths but redirects `place_order` / `cancel_order` to a Postgres `paper_trades` ledger. Selected automatically when `TRADING_MODE='paper'` (§8.9).

**Composition.** The `execution-engine` service holds exactly one `PredictionMarketAdapter` instance, selected at startup based on `TRADING_MODE`. The Trade Evaluation Team holds a read-only adapter (rejects write methods at the type-stub level). The Code Evaluation Team holds **no adapter** — it has no live-trading capability by design (`optimization.md §1`).

**Order types** (PA-aligned — orders execute immediately, no smart order routing in v1):

| Type | Use |
|---|---|
| Marketable limit | Default for both entry and exit. Buy at current best ask, sell at current best bid. |

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

**Slippage:** Realized fill price logged vs. expected (`q` at decision time). Sustained excess flagged as a `lesson` (`optimization.md §2`) for the operator. No pre-trade slippage gate in v1.

---

## 5. Logging & Observability

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
| Drawdown > 10% from peak | warn (operator decides whether to halt) |
| Drawdown > 15% from peak | critical (page operator) |
| Cycle latency P95 > 30s | warn |
| Reconciliation diff > $10 | critical |
| Error rate > 5% / 5 min | warn |
| Agent disagreement std > 0.30 sustained | info (regime change) |

---

## 6. Safety & Controls

**Kill switch:**
- Global Redis flag `system:kill_switch`.
- HTTP `POST /admin/kill` (auth: signed token + 2FA in prod).
- Order-placing services poll every 1s; on activation halt new orders. Existing positions remain open unless the operator explicitly closes them via `/manual override`.
- **Manual trigger only** (PA-aligned lean model): no automatic trip-wires. Alerts (§5) page the operator on drawdown / reconciliation diff / sustained errors; the operator decides whether to flip the switch. The only fully-automatic guard is the constitutional `MAX_CAPITAL_EUR` cap (§8.8), enforced inside every order-submit path.

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
- §6 kill switch + §2 risk limits unaffected by any evaluation-team action.

Result: no runaway self-modification. Human in loop on every merge that affects live behavior.

---

## 7. Capital-Allocation Feedback

`meta-allocator` (deterministic Python service, not a Claude team) rebalances per-agent capital weights weekly based on:
1. **Trading-performance** (rolling 30d hit rate + PnL — primary).
2. **Adaptation quality** — share of an agent's `lessons` that became merged proposals with positive paper outcome (small bonus, capped +10% of base allocation).

Rewards trading agents whose failure modes were genuinely informative — closes the loop between the Trading Team and the Code Evaluation Team. Allocation outputs are written to `agent_state.allocation_pct`, consumed by the `risk-engine` for per-agent sizing caps.

---

## 8. Engineering & AI-Coding Workflow

Sections above describe *what*; this section codifies *how* humans + AI build/modify the system.

### 8.1 Repository Layout

```
autonomous_trading/
├── research/        # strategies, agent prompts, skill library
│   ├── agents/      # per-agent persona + prompt + tools
│   └── skills/      # executable skill snippets (trading.md §8)
├── execution/       # order routing, CLOB client, position management
├── risk/            # limits, kill switches, sanity gates, capital gate
├── shared/          # data models, schemas, common utilities
│   └── config/      # central settings (§8.21)
├── infra/           # Docker, k8s, migrations, deployment
├── tests/           # mirrors src layout
├── docs/            # ADRs, runbooks, risk rules, code-eval decision records
├── .claude/         # project-scoped Claude Code config (committed)
├── CLAUDE.md        # AI-coding guard rails (committed)
├── CLAUDE.local.md  # personal notes (gitignored)
├── trading.md       # the trading runtime spec
├── infrastructure.md # this document
├── trading_feedback.md
├── optimization.md
└── specs.md         # entry point with architecture diagram
```

`risk/` is *protected code* — see §8.7.

### 8.2 CLAUDE.md (project)

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

### 8.3 GitHub Integration

- **Claude Code GitHub App** (`/install-github-app`): auto PR reviews, `@claude` mentions, fix pushes.
- **`gh` CLI** required locally — token-cheaper for AI use.
- **`/ultrareview`** before every merge into `main` touching `execution/` or `risk/`.
- **`/security-review`** on every PR touching auth, signing, or secrets.
- **GitHub Actions** with `claude -p` (headless): AI-code lint, regression detection on tests.
- **Branch protection on `main`**: required PR reviews (≥ 2 for `risk/`), required status checks (`tests`, `security-review`, `mypy-strict`, `gitleaks`), no direct pushes, no force-push.

### 8.4 Hooks (`.claude/settings.json`)

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
| `TaskCompleted` (production trading-cycle only) | Member marks task done | Validate output artifact against §8.12 Pydantic schema; exit 2 to force retry on schema mismatch; exit 0 only on clean artifact |
| `Stop` (production trading-cycle only) | Lead about to exit | Assert `clean up the team` was called; if not, force cleanup before allowing exit (Cloud-doc warning: avoid orphaned `~/.claude/teams/` entries) |

### 8.5 Subagents (`.claude/agents/`)

| Agent | Tools | Purpose |
|---|---|---|
| `strategy-researcher` | Read, Grep, WebSearch | Read-only strategy research; drafts paper-mode test plans |
| `risk-reviewer` | Read | Diff `risk/` and trading-decision paths against `docs/risk-rules.md` |
| `security-reviewer` | Read, Bash (scoped grep/gitleaks) | Secrets, injection, signing-path review |

### 8.6 Skills (`.claude/skills/`)

On-demand: `polymarket-api` (endpoints, rate limits, EIP-712 signing, fees); `paper-mode-protocol` (how to validate a strategy in paper mode, when to consider promotion to real-capital, see §8.9 / `trading_feedback.md §7`); `incident-response` (kill-switch, position-close, key rotation).

### 8.7 Risk Layer Protection

`risk/` contains the *deterministic* guard rails (size caps, daily loss caps, kill-switch triggers, capital gate). AI decides *what* to trade; risk layer decides *whether and how much*.

- All §2 limits = plain constants/config in `risk/`, never AI outputs.
- AI agents may not edit `risk/` outside Plan Mode + explicit user approval (§8.4 hook).
- All trading-decision paths import from `risk/`. Direct CLOB calls bypassing `risk/` forbidden by import-linter in CI.
- `risk/` requires 100% line coverage; CI fails below.

### 8.8 Capital Gate

Hardcoded absolute capital cap, single constant:

```python
# risk/capital_gate.py
MAX_CAPITAL_EUR: Final = <TBD_CAPITAL_CAP_EUR>
```

- `position-manager` rejects any order pushing gross deployed capital above `MAX_CAPITAL_EUR`.
- Constant changed only via: PR with required ≥ 2 reviewer approvals (branch protection), explicit rationale in PR description, audit-log entry on merge.
- Decreases also gated to ≥ 1 reviewer (prevent panic over-reduction).
- Reviewed quarterly.

### 8.9 Operational Modes (paper vs. real-capital)

The system has exactly two operational modes, controlled by a single flag in the central settings file (§8.21):

```
TRADING_MODE = "paper" | "real_capital"
```

- **`paper` (default).** Every step of the cycle runs identically — universe scan, agent inference, risk gates, sizing — except the `execution-engine` writes orders into a **paper-trading ledger** (Postgres table `paper_trades`) instead of routing them to Polymarket CLOB. Mark-to-market PnL uses live orderbook bids exactly like real mode. Settlement on resolution likewise. The §2 risk gates and `MAX_CAPITAL_EUR` apply to paper notional too — the simulation is intentionally faithful, including a paper "cash" balance that depletes with paper trades.
- **`real_capital`.** The `execution-engine` routes real orders to the Polymarket CLOB with EIP-712-signed transactions (§4). All other behavior identical to paper mode.

**Switching modes is manual.** Flipping `TRADING_MODE` is a code change in the central settings file: PR with ≥ 1 reviewer approval (real → paper, defensive direction) or ≥ 2 reviewer approvals (paper → real_capital, offensive direction); audit-log entry on merge. **Never set via env var, never set at runtime.** A human eyeballs the diff every time the system starts trading real capital.

**Default for new branches / fresh deploys.** `paper`. A clean checkout cannot trade real capital without an explicit settings-file change.

**Backtests.** Out of scope for v1 — Polymarket markets are too thin and too short-lived for a meaningful historical backtest harness. Paper mode replaces the backtest gate (see `trading_feedback.md §7` for paper-mode promotion guidance).

### 8.10 (reserved — paper-mode promotion is in `trading_feedback.md §7`)

### 8.11 Secret Management

- `.env*` in `.gitignore`, blocked by §8.4 hook on write.
- `gitleaks` + `trufflehog` in `.pre-commit-config.yaml`; same on `Stop` hook.
- Private keys never in repo/env: KMS or hardware only.
- API tokens in HashiCorp Vault or AWS Secrets Manager; fetched at startup, never persist to disk.
- Key rotation: `docs/runbooks/key-rotation.md`.

### 8.12 Strict Typing & Property-Based Tests

- `mypy --strict` is hard CI gate.
- All order/position/trade/decision/prediction/`PortfolioState` objects = Pydantic models. No untyped dicts on those paths.
- Risk-engine functions covered by `hypothesis` property tests, e.g. *"for any (proposed_notional, equity, open_orders), the clipped notional never exceeds 15% of equity AND never violates solvency"*.
- Coverage: `risk/` 100%, `execution/` ≥ 90%, rest ≥ 80%.

### 8.13 Custom Slash Commands (`.claude/commands/`)

| Command | Purpose |
|---|---|
| `/mode` | Print current `TRADING_MODE` and recent mode-switch history (read-only) |
| `/kill-all` | Trigger kill switch (with confirmation; `disable-model-invocation` set so only humans can run it) |
| `/risk-rules` | Print effective limits from `risk/` |
| `/audit <decision_id>` | Replay AI decision: prompt, model, tools, output, market data |

### 8.14 Permission Modes & Sandboxing

**Development sessions** (a human is at the terminal):
- `/permissions` allowlist for safe read-only/local commands (`pytest`, `git diff`, `ruff`, `gh pr view`).
- Auto-mode permitted only for `docs/`, `tests/`, read-only `research/` exploration.
- Auto-mode **forbidden** for any path that can reach live trading endpoints.
- `/sandbox` (OS-level) for any code with network access running unsupervised.
- For development against live-trading endpoints (e.g. running a one-off CLI), interactive permission confirmation is required every session — never allowlisted.

**Production trading-cycle session** (no human at the terminal — one fresh Claude Code Agent Team per cycle, ~12 min lifetime, per §8.22):
- The Lead is launched with `--dangerously-skip-permissions` so it does not block on prompts. This is unavoidable for unattended operation. Blast radius is bounded to the single cycle's lifetime — a misbehaving cycle cannot accumulate damage across cycles, and the next scheduled cycle starts clean.
- Safety in this mode comes from:
  1. **§8.4 hooks** — deterministic guard rails on tool use (block `rm -rf`, block writes to `risk/`, secret scanning, `SessionStart` janitor, `Stop` cleanup-assertion, etc.). Hooks fire regardless of permission mode.
  2. **§2 risk gates + §6 kill-switch** — the only authoritative gate on whether a trade is placed. The team's permission mode is irrelevant here; the risk-engine member can produce a `Decision`, but the deterministic position-manager (`risk/`) and execution-engine still enforce caps and refuse out-of-bounds orders. These services run as separate, long-lived processes — they do **not** die with the cycle.
  3. **§8.22 team hooks** — `TeammateIdle`, `TaskCreated`, `TaskCompleted` validate every artifact before downstream members consume it.
  4. **Network egress allowlist** — the production server can only reach Polymarket CLOB, Anthropic API, configured data sources; everything else is blocked at the firewall, so even a malformed tool call cannot exfiltrate or hit unintended endpoints.
  5. **Filesystem isolation** — production server writes only to repo-local `~/.claude/`, the Postgres/Redis network sockets, and S3 (scoped IAM). No general filesystem access.
  6. **Bounded lifetime** — every cycle process exits after ~12 min by design; a process that fails to exit is killed by the scheduler (e.g. cron + timeout, k8s `activeDeadlineSeconds`).
- The trading-cycle Lead is the **only** environment where `--dangerously-skip-permissions` is acceptable. Every other Claude Code session (dev, Trade-Evaluation runs, Code-Evaluation batches, debug) keeps standard permissions.

### 8.15 Workflow Discipline

- **Plan Mode** (Shift+Tab) for any non-trivial change.
- **Subagents** for investigation; keeps main context clean.
- `/clear` between unrelated tasks.
- `/rewind` / Esc-Esc to undo, never destructive bash recovery.
- **Writer/Reviewer split**: one session writes, fresh session reviews `execution/` + `risk/` changes.
- `/statusline` configured for live token usage.

### 8.16 Parallel Work Setups

- **Claude Code Desktop App** for multiple isolated worktrees in parallel.
- **Claude Code on the Web** for longer autonomous research on cloud VMs.
- **Agent Teams** for orchestrated multi-session workflows on complex refactors.

### 8.17 Audit Trail (development reinforcement of §5)

Every AI decision replayable from logs alone. Required per decision:
- Full input prompt (template + filled context)
- Model id + version + temperature/params
- Raw output (with tool calls + reasoning trace)
- Snapshot of market data used (orderbook, news bundle URI)
- All risk-gate evaluations + results
- Final action taken/rejected, with reason

Stored in `decisions` table (§3); reasoning blob in S3. Replay via `/audit <decision_id>`.

### 8.18 MCP Servers

`claude mcp add` for external systems used during *development*:
- Postgres/TimescaleDB (read-only for research, read-write only for migrations)
- Polymarket API (custom MCP if no public; read-only by default)
- Grafana/Sentry (observability into Claude session)
- Linear or Notion (strategy specs, ticket sync)

MCP servers are **never** wired into live trading-loop services — only developer/Claude sessions.

### 8.19 Development-Side Observability

Reinforces §5:
- Sentry for exceptions in all services.
- Grafana dashboard URL in PR template.
- Alerts on anomalous trade volume routed to operator phone (never to Claude/AI).

### 8.20 (reserved — open-source-first reuse policy is in `optimization.md §8`)

### 8.21 Centralized Configuration (Single Source of Truth)

**Hard rule.** All numerical thresholds, limits, parameters, and tunables that influence runtime behavior live in **one single settings file** — never duplicated, never hardcoded as magic numbers in service code. The user must be able to change every operational knob from one file.

- Canonical location: `shared/config/settings.py` (Pydantic Settings model) backed by `config/settings.toml` for the values themselves. Single source of truth.
- All services and agents import from this module — no parallel constants, no scattered defaults, no magic numbers in business logic.
- **Examples of values that MUST live there** (non-exhaustive): every limit in §2 (15% concentration cap, per-cycle spending cap), `TRADING_MODE` flag (§8.9), edge threshold (`trading.md §6`), cycle period and per-stage timeouts (`trading.md §5`), agent timeout, reconciler interval / diff threshold (§4), retry/backoff parameters (§4), fee estimator constants, top-K for belief injection (`trading.md §2`), notes cap, position-thesis adverse-move threshold (`trading.md §2`), Code-Eval explore/exploit budget ratios + drawdown-tilt and outperformance-tilt thresholds (`optimization.md §3`), weekly-batch top-K size (`optimization.md §3`), strategy-displacement minimum days (`trading.md §3.1`).
- **Risk-layer interaction (§8.7, §8.8).** The hardest gates (`MAX_CAPITAL_EUR`, kill-switch triggers, all `risk/`-owned limits) physically live inside `risk/` for protection. The settings file imports and re-exports them — it does **not duplicate** them. Users still see one file with all knobs; `risk/` retains its 2-human-review enforcement on the source.
- **Validation.** Pydantic Settings + `mypy --strict`: missing or wrong-typed values fail at service startup, never silently at runtime.
- **Change governance.** Edits to non-`risk/` settings require ≥ 1 reviewer + CI tests. Edits to `risk/`-owned values keep the §8.7/§8.8 stricter rules (≥ 2 reviewers, audit-log entry). `TRADING_MODE` paper → real_capital follows §8.9 review counts.
- **Hot-reload.** Non-critical operational knobs (scan filters, dashboard intervals) hot-reload from Postgres-backed config (§6) without restart. Structural values (cycle period, schema fields, `TRADING_MODE`) are restart-only.
- **Anti-pattern enforcement.** A CI lint rule rejects PRs that introduce a numeric literal in `execution/`, `research/`, or `risk/` outside the settings module (allowlist for trivial constants like `0`, `1`, `2`). Forces every new tunable through the settings file.

This rule is the dual of §8.7 risk-layer protection: §8.7 prevents the AI from changing the *hardest* limits without humans; §8.21 prevents anyone (human or AI) from scattering tunables so widely that no single file shows the full operating envelope.

### 8.22 Agent Teams & Subagents — Production Runtime + Dev Use

**Production trading loop = one fresh Claude Code Agent Team per cycle.** Every cycle (period from central settings, §8.21, default 12 min) the scheduler fires a new `claude` process. The Lead boots the team, runs the cycle (`trading.md §5`), calls `clean up the team`, and exits. The next scheduled cycle starts a brand-new process with no in-process state from the previous one. Long-term memory persists in Postgres / S3; short-term memory dies with the process. This makes the §1 *"memory is the only coupling between tasks"* invariant **enforced by construction**, not by trusting a long-lived Lead to reset state correctly.

**Why per-cycle (rationale):**
- Bug isolation: a misbehaving cycle cannot poison the next.
- Memory leaks impossible (process dies).
- Cloud-doc limitations of the experimental feature (no session resumption, fixed Lead, one team per session) become non-issues — every cycle is a fresh session by design.
- Claude Code version upgrades are picked up automatically at the next cycle. If an upgrade breaks the team, exactly one cycle fails and the next one runs the previous (pinned) version after rollback.
- Failure handling is trivial — a failed cycle does not run; the next one tries again. No supervisor with liveness probes, no auto-restart logic.

**Bootstrap (single command per cycle, run by scheduler, not by a human):**

```bash
# .claude/settings.json sets CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 (§8.4).
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
2. The `Stop` hook (§8.4) asserts cleanup was called; if not, forces it before allowing process exit.
3. The next cycle's `SessionStart` hook (§8.4) sweeps any stale `~/.claude/teams/` directories left by a crashed prior cycle, before spawning the new team.

**Hooks specifically for the team** (in `.claude/settings.json`, see §8.4 table for full set):
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

**Versioning & rollback.** Claude Code version pinned in `infra/` (e.g. via Docker image digest, not a floating tag). Team-spec source-of-truth (member roster, subagent definitions, system prompts) lives in `.claude/agents/` + `.claude/teams/trading-team.spec.json` in the repo. The runtime `config.json` written by Claude Code is ephemeral per cycle and not edited by hand (Cloud-doc rule). Any change to the team spec is a normal PR through §8.3 branch protection. If a Claude Code release introduces an Agent-Teams-breaking change, exactly one cycle fails before rollback; paper-mode catches this before prod via the upgrade-validation cycles run there.

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

- `trading.md` — the trading runtime that consumes this infrastructure.
- `trading_feedback.md` — Tier 1 evaluation, paper-mode promotion guidance.
- `optimization.md` — Tier 2 code evaluation, governance for code changes (§7 review counts).
- `specs.md` — architecture diagram and entry point.
