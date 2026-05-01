# Data Infrastructure — Storage, Market Interface, Observability

Where data lives, how the prediction market is accessed, and how the system is observed. Owns: data sources, all storage tiers, all long-term-memory schemas, retention, the Polymarket adapter (Kalshi post-MVP), order placement mechanics, idempotency + reconciliation, failure handling, slippage tracking, and the full observability stack.

**What lives here:** schemas, sources, adapters, observability. **What does not live here:** runtime topology (`orchestration.md`), risk limits or safety controls (`engineering.md §1`–§2), secrets (`engineering.md §7`) or central settings (`engineering.md §10`).

---

## MVP scope (this version of the doc)

To start as simply as possible:
- **Single market venue:** Polymarket only (CLOB + Gamma). Kalshi and any multi-venue support are deferred until post-MVP.
- **Single information-intake channel for everything that is not market data:** OpenAI's web search (Responses API + `web_search_preview`), invoked via the `web_search` skill. **No** NewsAPI, GDELT, X/Twitter, Reddit, Tavily/Brave, FRED, or per-domain feeds in MVP. The agent searches the web through one tool and reads the synthesized result.
- **Internal data:** market data from Polymarket, plus our own resolved-market archive for performance tracking. Nothing else.
- **Single storage tier:** Postgres 16 only. No TimescaleDB, no Redis, no S3/MinIO in MVP. Time-series tables are plain Postgres (snapshot cadence is per-cycle, not per-second — comfortably handled). Reasoning traces, research blobs, and any binary-ish artifacts go into JSONB columns. Kill-switch and rate-limit state live in a small `system_state` table or in-process counters. Anything that was previously a Redis or S3 concern is folded back into Postgres for MVP.

Anything below that contradicts this MVP scope is aspirational and applies post-MVP.

---

## 0. Build on existing OSS first (load-bearing principle)

Before writing any adapter, data-fetcher, market-client, parser, signer, or pipeline component: **search GitHub / PyPI for an existing project and build on it.** Do not reimplement what is already maintained. Specifically — but not exclusively — relevant to the components specified below:

| Component | Existing projects to evaluate first |
|---|---|
| Polymarket CLOB client (REST + WS, EIP-712 signing) | `Polymarket/py-clob-client` (official Python client) |
| Polymarket Gamma API client | The official Polymarket monorepo + community wrappers; raw REST is fine if no library fits |
| Web search (the MVP intake channel) | OpenAI Responses API with `web_search_preview` tool — Prediction-Arena reference |
| EIP-712 signing | `web3.py` (eth_account); deferred until first `real_capital` switch |
| Observability instrumentation | `structlog` for structured logs |
| Reconciliation patterns / order idempotency | Reference reads: freqtrade or Hummingbot — patterns only, not deps |

Post-MVP additions (Kalshi clients, time-series helpers, Sentry/OpenTelemetry, multi-source news SDKs) follow the same workflow once they enter scope.

**Workflow:**
1. Before opening a PR that adds an integration, link the upstream OSS project considered (or rejected, with reason) in the PR description.
2. Prefer thin wrappers/adapters over the OSS lib — keep our `shared/adapters/` Protocols as the abstraction boundary, the OSS lib lives behind that boundary.
3. Pin OSS versions in `pyproject.toml`; never vendor copies into the repo.
4. If we genuinely have to roll our own (the OSS lib is dead, unsafe, or wrong-shape), document the decision as an ADR in `docs/adr/`.

This rule is the dual of `engineering.md §10`: §10 prevents tunable-sprawl, §0 prevents *implementation*-sprawl. Both keep the system small enough for a solo operator to maintain.

---

## 1. Data Layer

**Sources (MVP):**

| Source | Type | Use |
|---|---|---|
| Polymarket CLOB API (WS + REST) | Market | Orderbook, trades, fills |
| Polymarket Gamma API | Market metadata | Resolution criteria, end dates, categories |
| OpenAI web search (Responses API + `web_search_preview`) | Research | **The only news/social/macro/web-context intake.** Invoked via `research/skills/web_search.py`. Replaces every other open-web source for the MVP. |
| Internal: resolved markets archive | Performance tracking | Per-agent hit-rate / PnL evaluation |

**Post-MVP candidates** (none active in MVP): NewsAPI / GDELT, X / Twitter, Reddit, Tavily / Brave, Kalshi public API, FRED, sports stats APIs. Each is added only when a measured gap in MVP performance demands it, and goes through the §0 OSS-first workflow.

**Storage (MVP):**

| Store | Tech | Data |
|---|---|---|
| Single tier | Postgres 16 | The 11 MVP tables: `markets`, `market_snapshots`, `predictions` (with `inference_log` JSONB carrying the reasoning trace), `decisions`, `trades`, `paper_trades`, `positions`, `notes`, `cycle_plan`, `lessons`, `system_state`. Kill-switch flag and any global runtime flags live in `system_state`. Rate-limit counters are in-process per service (no persistence). Post-MVP tables (`agent_performance`, `beliefs`, `operating_doctrine`, `patterns`, `proposals`) — see "Post-MVP table sketches" subsection below. |

**Post-MVP candidates:** TimescaleDB hypertables for sub-second `market_snapshots`, Redis for hot state and pub/sub, S3 for cold reasoning-trace archive and screenshots. Each is added when a measured limit (table size, query latency, snapshot cadence) demands it.

**Key MVP schemas (sketch):**

```sql
markets(id pk, condition_id, slug, title, category, end_date, status,
        resolution_source, created_at, last_seen, ambiguity_score)

market_snapshots(time, market_id fk, best_bid, best_ask, mid, depth_bid_1pct,
                 depth_ask_1pct, volume_24h)              -- plain Postgres table in MVP,
                                                          -- per-cycle cadence; index on (market_id, time)

predictions(id pk, time, market_id fk, agent_id, p_raw,
            inference_log jsonb,                          -- prompt + Claude output + tool calls + web_search results
            latency_ms,
            outcome?, pnl_realized?)                       -- outcome/pnl set by outcome-ingestion script (trading_feedback.md §1)

system_state(key text pk, value jsonb, updated_at)        -- kill_switch, mode flags, ad-hoc runtime state

decisions(id pk, cycle_id, market_id fk, p_consensus, q_market, edge,
          gate_results jsonb, action, rationale)

trades(id pk, decision_id fk, time, market_id fk, side, size, price, fees,
       status, broker_order_id, parent_trade_id,
       realized_pnl?)                                      -- realized_pnl set on settlement

paper_trades(id pk, decision_id fk, time, market_id fk, side, size, price, fees,
             status, broker_order_id?,                     -- broker_order_id null in paper mode
             parent_trade_id, realized_pnl?)               -- mirror of `trades`; populated when TRADING_MODE='paper'

positions(market_id fk, side, size, avg_price, unrealized_pnl, realized_pnl,
          opened_at, last_updated, status)                 -- single positions ledger across paper / real_capital,
                                                          -- distinguished by upstream trade source

notes(id pk, time, agent_id, body, tags jsonb, last_accessed)   -- per-agent scratchpad (LRU ≤ 50 rows in MVP)

cycle_plan(id pk, written_at, written_by_cycle_id,
           next_priorities jsonb, holds_with_rationale jsonb,
           pending_settlements jsonb, opportunities_deferred jsonb,
           blockers jsonb, superseded_at?)                -- portfolio-level forward-looking handoff;
                                                         -- exactly one row with superseded_at IS NULL

lessons(id pk, time, source_agent_id, trigger_event_id, market_id?,
        observation, hypothesis, action_taken, outcome, status,
        parent_pattern_id?, tags jsonb)            -- append-only; written by lessons-summary script (optimization.md §1)
```

**Post-MVP table sketches** (deferred — full specs in `trading_feedback.md §6`, `optimization.md §5`, `trading.md §8`):

```sql
agent_performance(agent_id, time, hit_rate_30d, sharpe_30d, pnl_30d, n_samples)
                                              -- written by Trade Evaluation Team (trading_feedback.md §6);
                                              -- not needed in MVP because there is only one trading agent

beliefs(id pk, time, agent_id, domain,
        scope ('global'|'category'|'market'|'position'),
        scope_ref, statement, p_estimate?, confidence, evidence_uri,
        supersedes_id?, status, last_updated)             -- per-agent structured views (trading.md §8)

operating_doctrine(id pk, created_at, target_date,
                   phases jsonb, current_phase, key_risks jsonb,
                   revised_at, revised_by, approved_by,
                   supersedes_id?, status)                -- current operative strategy with lineage (trading.md §8);
                                                         -- exactly one row with status='active'

patterns(id pk, first_seen, last_seen, occurrences, description,
         supporting_lesson_ids uuid[], confidence)        -- written by Code Evaluation Team (optimization.md §5)

proposals(id pk, time, source_agent_id,
          target_kind ('prompt' | 'strategy' | 'code' | 'limit' | 'config'
                       | 'operating_doctrine' | 'agent_roster'),
          target_ref, current_value, proposed_value, rationale,
          paper_validation_uri, pr_url?, status, decided_by, decided_at)
                                                         -- written by Code Evaluation Team (optimization.md §5)
```

**Retention (MVP):**
- `market_snapshots`: 30 days, then a daily cron prunes older rows.
- `predictions`, `decisions`, `trades`, `paper_trades`, `positions`: indefinite.
- `predictions.inference_log` (reasoning trace JSONB): 90 days, then nulled out (the prediction row stays, the blob drops).
- `cycle_plan`: latest active row hot; superseded rows kept 90 days, then deleted.
- `lessons`: indefinite.
- Retention for post-MVP tables specified when those tables enter scope.

Post-MVP: cold-archive policies (S3 / Glacier) once Postgres size becomes a concern.

---

## 2. Prediction-Market Interface (adapter abstraction)

**MVP:** Polymarket is the **only** venue. The Protocol below still exists so post-MVP additions (Kalshi, etc.) are additive changes, not rewrites — but no other adapter ships in MVP.

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

**MVP implementations:**

- **`PolymarketAdapter`** — primary. WebSocket subscribed to orderbooks for tracked markets (open positions + active candidates). REST for orders + account state. EIP-712 typed-data signing via the `KeyProvider` abstraction (`engineering.md §7`): AWS KMS preferred, encrypted-at-rest local file acceptable fallback, plaintext keys forbidden. Network: Polygon mainnet; gas in MATIC. Settlement: USDC.e. Polymarket Gamma API for resolution lookup. **Full read-and-write** — used by Trading Team in `real_capital` mode.
- **`PaperTradingAdapter`** — wraps `PolymarketAdapter` for read paths but redirects `place_order` / `cancel_order` to a Postgres `paper_trades` ledger. Selected automatically when `TRADING_MODE='paper'` (`engineering.md §4`).

**Post-MVP:** `KalshiAdapter` (read-only cross-venue reference) — added only if and when an explore-track strategy needs it.

**Composition (MVP).** The `risk-execution` member of the Trading Team (`trading.md §2`) instantiates exactly one `PredictionMarketAdapter` per cycle, selected at startup based on `TRADING_MODE`. Other members (`scanner-reviewer` for read-only universe/orderbook, `trading-agent` for analysis) only need read paths and use the same adapter instance via the Lead. The outcome-ingestion script (`trading_feedback.md §1`) instantiates its own read-only Polymarket Gamma client (no order endpoints). The lessons-summary script (`optimization.md §1`) holds **no adapter** — it has no live-trading capability by design. Post-MVP Trade-Evaluation-Team and Code-Evaluation-Team adapter rules are specified in `trading_feedback.md §6` and `optimization.md §5`.

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

## 3. Logging & Observability

**Structured logs (JSON to stdout → file in MVP; cloud-side aggregator post-MVP).** Required per line: `timestamp, service, level, cycle_id, correlation_id, market_id?, agent_id?, decision_id?, message, ...payload`. Reasoning traces (full prompt + Claude output + tool calls + web_search results) live in `predictions.inference_log` JSONB — every decision is reconstructable from Postgres + the structured log alone.

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
| `agent_hit_rate_30d` | gauge | agent_id (post-MVP — single trading agent in MVP) |
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
| Agent disagreement std > 0.30 sustained | info (regime change) — post-MVP, requires ensemble |

---

## See also

- `orchestration.md` — runtime topology that consumes these schemas and adapters.
- `engineering.md` — risk limits, safety controls, secrets, central settings, audit trail (development reinforcement of §3 above).
- `trading.md` — what fills the schemas in §1.
- `trading_feedback.md` — outcome-ingestion script that writes `outcome` and `realized_pnl` (MVP); the full Trade Evaluation Team is post-MVP (§6).
- `optimization.md` — daily lessons-summary script that writes `lessons` (MVP); the Code Evaluation Team that writes `patterns` / `proposals` is post-MVP (§5).
- `specs.md` — architecture diagram and entry point.
