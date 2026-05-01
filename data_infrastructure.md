# Data Infrastructure — Storage, Market Interface, Observability

Where data lives, how the prediction market is accessed, and how the system is observed. Owns: data sources, all storage tiers, all long-term-memory schemas, retention, the Polymarket / Kalshi adapter abstraction, order placement mechanics, idempotency + reconciliation, failure handling, slippage tracking, and the full observability stack.

**What lives here:** schemas, sources, adapters, observability. **What does not live here:** runtime topology (`orchestration.md`), risk limits or safety controls (`engineering.md §1`–§2), secrets or central settings (`engineering.md §12`, §21).

---

## MVP scope (this version of the doc)

To start as simply as possible:
- **Single market venue:** Polymarket only (CLOB + Gamma). Kalshi and any multi-venue support are deferred until post-MVP.
- **Single information-intake channel for everything that is not market data:** OpenAI's web search (Responses API + `web_search_preview`), invoked via the `web_search` skill. **No** NewsAPI, GDELT, X/Twitter, Reddit, Tavily/Brave, FRED, or per-domain feeds in MVP. The agent searches the web through one tool and reads the synthesized result.
- **Internal data:** market data from Polymarket, plus our own resolved-market archive for performance tracking. Nothing else.

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

This rule is the dual of `engineering.md §21`: §21 prevents tunable-sprawl, §0 prevents *implementation*-sprawl. Both keep the system small enough for a solo operator to maintain.

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

- **`PolymarketAdapter`** — primary. WebSocket subscribed to orderbooks for tracked markets (open positions + active candidates). REST for orders + account state. EIP-712 typed-data signing; private key in cloud KMS or hardware key (YubiHSM). Network: Polygon mainnet; gas in MATIC. Settlement: USDC.e. Polymarket Gamma API for resolution lookup. **Full read-and-write** — used by Trading Team in `real_capital` mode.
- **`PaperTradingAdapter`** — wraps `PolymarketAdapter` for read paths but redirects `place_order` / `cancel_order` to a Postgres `paper_trades` ledger. Selected automatically when `TRADING_MODE='paper'` (`engineering.md §11`).

**Post-MVP:** `KalshiAdapter` (read-only cross-venue reference) — added only if and when an explore-track strategy needs it.

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

## 3. Logging & Observability

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

## See also

- `orchestration.md` — runtime topology that consumes these schemas and adapters.
- `engineering.md` — risk limits, safety controls, secrets, central settings, audit trail (development reinforcement of §3 above).
- `trading.md` — what fills the schemas in §1.
- `trading_feedback.md` — Tier 1 evaluation that writes outcomes and PnL.
- `optimization.md` — Tier 2 evaluation that writes lessons / patterns / proposals.
- `specs.md` — architecture diagram and entry point.
