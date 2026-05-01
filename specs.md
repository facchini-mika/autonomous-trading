# Polymarket Autonomous Trading System — Specifications

## 1. Objective

Deploy capital on Polymarket binary prediction markets to generate risk-adjusted returns by systematically identifying and exploiting probabilistic mispricings. A heterogeneous ensemble of AI agents produces calibrated probability estimates compared against market-implied probabilities to extract expected-value edge.

**Core pattern.** This is an **agent-orchestration system**, not a single LLM with plumbing. Specialized agents perform distinct tasks (research, prediction, aggregation, risk gating, execution, reflection), pass typed memory artifacts within a cycle (§2), and accumulate cross-cycle memory (§15). Decisions emerge from the pipeline; outcomes feed back into memory.

**Primary KPIs:**
- Net Sharpe > 1.5 (annualized, post fees + gas)
- Max drawdown < 25% of equity
- Net PnL > 0 monthly; positive trailing 90d return
- Edge consistency: no single category > 40% of PnL

**Out of scope (v1):** non-binary scalar markets, leverage beyond bankroll, market-making, externally-pooled capital.

---

## 2. System Architecture

**Tier 1 — Per-cycle pipeline.** Sequence of tasks, each owned by an agent/service. Memory passed as typed artifacts and persisted (fully replayable):

```
[market-scanner]           → universe of candidate markets
[research-orchestrator]    → ResearchBundle  (news, history, related markets; in S3)
[trading agents — §4]      → Prediction      (p_yes, confidence, reasoning) per agent
[aggregator]               → ConsensusProbability  (calibrated, with disagreement metric)
[risk-engine]              → Decision        (action, size, gate results, rationale)
[execution-engine]         → Trades + fills
[evaluator (on resolve)]   → Outcome + PnL + per-agent calibration update
```

**Tier 2 — Cross-cycle reflection (§15).** Improvement agents read Tier-1 memory asynchronously and emit higher-order memory that flows back via prompt/strategy/config updates after human approval:

```
predictions ┐
decisions   ├──→  lessons  ──→  patterns  ──→  proposals  ──→  Git PRs / config / prompt updates
trades      │    (append)      (curated)      (gated)
outcomes    ┘
```

**Memory taxonomy (8 layers):**

| Layer | Where | Lifetime | Purpose |
|---|---|---|---|
| Working | Research bundle in S3; in-flight prediction in process memory | one cycle | Pass context within a cycle |
| Agent notes | Per-agent `notes` table (max 50 × ~200 words, LRU) | cross-cycle, capped | Trading agent's scratchpad — ad-hoc reminders + provisional flags |
| Agent beliefs | Per-agent `beliefs` table, typed by domain | cross-cycle, revisable, full lineage | Structured market views — calibration-tracked (§4 + §8) |
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

**Decision cycle (T = cycle start):**

1. **T+0s — Universe snapshot.** Filter: 24h vol ≥ $10k; bid-ask depth ≥ $1k within ±1% of mid; end date > 24h and < 365d; not in cooldown.
2. **T+15s — Triage.** Cheap heuristic edge estimate (price vs base rate, related markets, momentum) ranks markets. Top-K (K=20) advance.
3. **T+30s — Research dispatch.** Per candidate: parallel bundle (news 7d, web search 3–5 queries, historical analogues, related-market snapshot). Persisted to S3, ID forwarded.
4. **T+3m — Agent inference.** Each agent gets bundle + market metadata. Output: `P(YES)`, confidence, reasoning trace URI. Parallel; per-agent timeout 90s. Failed agents excluded from this market only.
5. **T+5m — Calibration & aggregation.** Per-agent isotonic recalibration; hit-rate-weighted mean. Consensus confidence = weighted geometric mean × disagreement penalty.
6. **T+6m — Edge computation.** Live orderbook. Compute `q = mid`, `effective_q` = volume-weighted price for intended size, `edge = p_consensus − effective_q` (symmetric for NO).
7. **T+7m — Risk gates.** Order: edge threshold → position limits → category exposure → drawdown → slippage → resolution risk. Reject or size-reduce.
8. **T+8m — Sizing.** Quarter-Kelly within remaining caps.
9. **T+9m — Order placement.** Limit, post-only when viable. Iceberg slicing for size > $500.
10. **T+10–11m — Fill monitoring.** Track partial fills. Cancel/replace on > 1% adverse move.
11. **T+12m — Cycle close.** Persist decisions, fills, reasoning links. Emit metrics. Next cycle.

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

**Per-cycle prompt context** (assembled deterministically, on top of system prompt with role/philosophy/risk/tools/protocol):
- Current timestamp + cycle metadata
- Market data: orderbook snapshot, settlement criteria, depth at ±1% of mid
- Account state: cash, positions, unrealized PnL
- **Recent settlements** — last 10 resolved markets with realized PnL
- **Recent closed trades** — last 10 trades with realized PnL
- **Previous-cycle reasoning** — agent's own prior reasoning on same market/category
- **Critical-learning section** — curated `lessons`/`patterns` excerpts (§15.2): losing patterns to avoid, winning patterns to replicate, position-management reminders. Curated weekly by `meta-reviewer`; injected per cycle by research-orchestrator.
- The agent's most recent `notes`
- Step-by-step trading protocol

This is the explicit bridge between Tier-1 working memory and Tier-2 reflective memory. Every decision is conditioned on a defined, audit-inspectable slice of past experience.

**Agent notes (per-agent scratchpad).** Private `notes` store via `manage_notes` tool (read/write/search/edit). Bounded: max 50 × ~200 words, LRU. Used by the agent itself for patterns spotted during reasoning, reminders for next cycle, tentative hypotheses too provisional for `lessons`. Private to one agent (no cross-agent leakage during prediction — preserves §4 independence) but readable by improvement agents (§15) for pattern mining.

**Agent beliefs (structured market views).** Typed `beliefs` store via `manage_beliefs` tool (`create`, `revise`, `retire`, `search`). A belief is a structured statement the agent currently holds. Fields:
- `domain` — `market_structure` / `strategy` / `event` / `risk` / `sentiment`
- `scope` — `global` / `category` / specific market id
- `statement` — short claim, ≤ 100 words
- `p_estimate` — optional probability for falsifiable beliefs (e.g. *"Fed pauses in December"* → P ≈ 0.7)
- `confidence` — agent's own confidence
- `evidence_uri` — pointer to grounding bundle/news/source
- `supersedes_id` — id of prior belief revised (lineage preserved; nothing deleted)
- `status` — `active` / `superseded` / `retired`

**Beliefs vs notes vs lessons vs predictions:**

| Layer | Granularity | Author | Lifetime | Use |
|---|---|---|---|---|
| `predictions` (§8) | per-market, per-cycle | trading agent | event-bound | Direct probability now; consumed by aggregator |
| `notes` (§4) | free-form | trading agent | LRU-capped | Ad-hoc scratchpad, no schema |
| `beliefs` (§4) | typed, structured | trading agent | revisable, history kept | First-class views the agent operates *under* |
| `lessons` (§15.2) | post-hoc observation | improvement agent | append-only | Reflective, written *about* the system |

**Belief usage:**
- Injected per cycle as *"your active beliefs about this market/category"* — top-K by relevance + recency.
- Falsifiable beliefs (with `p_estimate`) are performance-tracked: hit rate on belief outcomes alongside per-cycle predictions (§10).
- Settlement/move that contradicts an active belief auto-flags it next cycle; agent decides `revise` (new belief with `supersedes_id`) or `retire`.
- Improvement agents (§15) mine beliefs across the agent population for convergent/divergent views, surface stale beliefs, lift recurring true beliefs into shared `patterns`.

This makes the agent's *implicit world model* explicit, inspectable, and calibration-trackable.

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
| Relational | Postgres 16 | `markets`, `predictions`, `decisions`, `trades`, `positions`, `agent_state` |
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

beliefs(id pk, time, agent_id, domain, scope, scope_ref,
        statement, p_estimate?, confidence, evidence_uri,
        supersedes_id?, status, last_updated)             -- per-agent structured views

notes(id pk, time, agent_id, body, tags jsonb, last_accessed)   -- per-agent scratchpad (LRU)
```

**Retention:**
- Snapshots: 90d hot → continuous-aggregate to 1h cold for 5y.
- Trades, decisions, predictions: indefinite.
- Reasoning traces: 1y hot → cold S3.

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
- Anthropic API outage (Claude Opus): no model failover by design. Affected agents abstain for current cycle. Outage > 5 min: system halts new-order placement cycle-wide, reverts to monitor-only until recovery; existing positions governed by deterministic rules in `risk/` (stop-outs, kill-switch, time-based close).
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
- All order/position/trade/decision/prediction objects = Pydantic models. No untyped dicts on those paths.
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

- `/permissions` allowlist for safe read-only/local commands (`pytest`, `git diff`, `ruff`, `gh pr view`).
- Auto-mode permitted only for `docs/`, `tests/`, read-only `research/` exploration.
- Auto-mode **forbidden** for any path that can reach live trading endpoints.
- `/sandbox` (OS-level) for any code with network access running unsupervised.
- Live-trading endpoint use requires interactive permission confirmation every session — never allowlisted.

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
| `strategy-improver` | Read losing trades + new patterns; propose prompt/strategy/sizing deltas | Weekly + after drawdown trip-wire (§6) | `proposals` row + draft PR with backtest |
| `prior-art-scout` | Enforce §14.20 — search GitHub/PyPI/papers before custom builds | On every new-component PR open | Prior-art note posted to PR |
| `meta-reviewer` | Aggregate, dedupe, prioritize all proposals; route top-K to operator | Weekly | Decision queue (Slack/email) |

All defined as Claude Code subagents (§14.5) with read-only access to live observational data (`predictions`, `decisions`, `trades`, snapshots, calibration history) and write access only to `lessons`, `patterns`, `proposals`, and Git via PR on feature branches.

### 15.2 Shared Memory & Notes

Three additive tables (on top of §8):

```sql
lessons(id pk, time, source_agent_id, trigger_event_id, market_id?,
        observation, hypothesis, action_taken, outcome, status,
        parent_pattern_id?, tags jsonb)            -- append-only

patterns(id pk, first_seen, last_seen, occurrences, description,
         supporting_lesson_ids uuid[], confidence)

proposals(id pk, time, source_agent_id,
          target_kind ('prompt' | 'strategy' | 'code' | 'limit' | 'config'),
          target_ref, current_value, proposed_value, rationale,
          backtest_result_uri, status, decided_by, decided_at)
```

- `lessons` append-only; supersedence via `status`, never deletion. Replay always possible.
- `patterns` curated by `pattern-miner`; references the lessons that built it.
- `proposals` drives the change pipeline (§15.4).

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
- **Working memory** — context that lives only for one trading cycle (research bundle, in-flight prediction).
- **Episodic memory** — per-event durable records (predictions, decisions, trades, positions) used for replay/audit.
- **Reflective memory** — observations + hypotheses written by improvement agents into `lessons` after the fact.
- **Belief** — typed structured statement an agent currently holds about how a market or class behaves. In `beliefs` table; performance-tracked (hit rate) when falsifiable; revisions preserve full lineage. Distinct from `predictions` (per-market per-cycle), `notes` (ad-hoc), `lessons` (post-hoc, by improvement agents).
- **Dual knowledge management** — pattern of giving an agent both unstructured `notes` and structured `beliefs` as separate memory stores, each with its own tool API + lifetime. Adopted from Prediction Arena.
