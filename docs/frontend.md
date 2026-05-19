# Frontend Command Center — Builder Brief

> **Audience:** an implementer (likely a fresh Claude Code session) who will
> scaffold the read-only command center described below. This is the complete
> spec. You should not need to ask the operator clarifying questions to start.
>
> **Reference visual:** Prediction Arena, specifically the model detail page
> e.g. `https://www.predictionarena.ai/models/claude-opus-4-6` and the
> homepage leaderboard. We mimic its dark, grain-textured, account-value-
> centric aesthetic. We **do not** mimic its multi-agent leaderboard at MVP —
> see "Multi-agent" below.

---

## A. Goal & non-goals

**Goal.** A local, read-only web UI that lets the single operator of this
repo answer, at a glance and without touching SQL or logs:

1. *What is the agent doing right now?* — current trading mode, kill-switch
   state, last cycle, current positions, today's PnL.
2. *Why did it do that?* — for any cycle, the full reasoning chain from
   universe scan to order placement.
3. *How is it doing over time?* — equity curve, cumulative trades, win rate,
   max drawdown.

**Non-goals (do not implement).**

- Order placement, order cancellation.
- Kill-switch toggle. Manual approval flows for `requires_approval` decisions.
- Switching `TRADING_MODE` from `paper` to `real_capital`.
- Auth, multi-tenant, public hosting.
- Mobile-first layout (desktop-first; responsive collapse is fine).
- WebSocket / SSE live ticking. Polling is acceptable.
- Calibration / Brier / ECE charts — we do not currently track these
  (see project Memory `reference_prediction_arena`).
- Backtest replay UI.

---

## B. Architecture

Two services, both bound to `127.0.0.1`. Localhost-only — never expose
without adding auth, the dashboard leaks live strategy and positions.

```
┌──────────┐    ┌──────────────┐    ┌────────────────┐    ┌─────────┐
│ Postgres │ ←  │ FastAPI :8000│ ←  │ Next.js :3000  │ ←  │ Browser │
│  (read)  │    │  (read-only) │    │  (App Router)  │    │         │
└──────────┘    └──────────────┘    └────────────────┘    └─────────┘
                  src/api/             apps/web/
```

- **FastAPI** lives at `src/api/` (new package). It re-uses the existing
  SQLAlchemy models in `src/shared/models/` and the session factory in
  `src/shared/adapters/db.py`. No business logic — endpoints are pure SQL
  reads + DTO mapping. Pydantic response schemas live in `src/api/schemas.py`.
- **Next.js 14 (App Router)** lives at `apps/web/` (new package, npm/pnpm
  workspace at repo root not required — a single sub-tree is fine). Tailwind
  + shadcn/ui. Server Components fetch from FastAPI; Client Components only
  for interactive bits (charts, range toggles, tabs).
- **No new DB tables, no migrations.** The frontend reads what already
  exists.
- **No code under `src/risk/**` is touched.** This is a hard rule from
  `CLAUDE.md` — Plan-Mode requirement applies to that subtree.

---

## C. Data sources (what the agent already persists)

Postgres tables already in place (verified against
`alembic/versions/0001_initial_schema.py` and `0004_subagent_audit_trail.py`):

| Table | Purpose | Key fields |
|---|---|---|
| `markets` | market catalogue | `market_id`, `slug`, `title`, `category`, `end_date`, `status`, `resolution_source`, `ambiguity_score` |
| `market_snapshots` | orderbook history | `time`, `market_id`, `best_bid`, `best_ask`, `mid`, `depth_bid_1pct`, `depth_ask_1pct`, `volume_24h` |
| `predictions` | trading-agent outputs | `id`, `market_id`, `agent_id`, `p_yes` (0..1), `reasoning`, `edge`, `inference_log` (JSONB), `latency_ms`, `outcome`, `realized_pnl`, `created_at` |
| `decisions` | risk-execution outputs | `id`, `cycle_id`, `market_id`, `p_consensus`, `q_market`, `edge`, `gate_results` (JSONB), `action` ∈ `{trade, skip, hold}`, `rationale`, `created_at` |
| `trades` | real-capital fills | `id`, `decision_id`, `market_id`, `side`, `size`, `price`, `notional_usd`, `fees`, `gas`, `status`, `broker_order_id`, `parent_trade_id`, `realized_pnl`, `created_at`, `filled_at` |
| `paper_trades` | identical schema, paper mode | (same as `trades`) |
| `positions` | open & closed positions | `market_id`+`side` (PK), `size`, `avg_price`, `unrealized_pnl`, `realized_pnl`, `opened_at`, `last_updated`, `status` |
| `cycle_plan` | forward-looking plan per cycle | `id`, `written_at`, `written_by_cycle_id`, `next_priorities`, `holds_with_rationale`, `pending_settlements`, `opportunities_deferred`, `blockers`, `superseded_at` |
| `notes` | agent-written context notes | `id`, `agent_id`, `body`, `tags`, `last_accessed`, `created_at` |
| `lessons` | agent-derived lessons | `id`, `source_agent_id`, `market_id`, `observation`, `hypothesis`, `action_taken`, `outcome`, `status`, `created_at` |
| `subagent_runs` | per-subagent audit trail | `id`, `cycle_id`, `agent_name`, `latency_ms`, `cost_usd`, `usage`, `task_payload`, `envelope`, `error`, `created_at` |
| `web_search_calls` | web search audit | `id`, `run_id`, `cycle_id`, `query`, `summary`, `hits`, `model_used`, `elapsed_sec`, `error`, `created_at` |
| `system_state` | KV system flags | `key` ∈ `{kill_switch, last_cycle_ts, last_outcome_ingestion_at, last_lessons_summary_at}`, `value` (JSONB) |

**Important nuance:** `TRADING_MODE` is **not** persisted per cycle in DB —
it lives in `src/shared/config/settings.py` (loaded from `.env`). The API
reads it from settings at request time and exposes it via
`GET /agents/{id}/status`.

**Cycle identity:** there is no dedicated `cycle_runs` table. A cycle is
identified by `cycle_id` (string of form `cycle-<unix_ts>`) which appears in
`decisions.cycle_id`, `subagent_runs.cycle_id`, `web_search_calls.cycle_id`,
and `cycle_plan.written_by_cycle_id`. Build the cycle index by selecting
distinct `cycle_id` from `subagent_runs` (it's the most complete table).

**Trade source selection:** in paper mode the agent writes to `paper_trades`,
in real_capital mode to `trades`. The API endpoint must read the table
matching the **current** `TRADING_MODE` and label each row accordingly. Do
not UNION blindly — the operator may run paper for weeks then flip; both
histories should remain inspectable but visually distinguished.

---

## D. API surface (FastAPI, all read-only)

All endpoints accept `?agent_id=<id>` (default = `settings.default_agent_id`,
typically `"trading-agent"`). All responses are JSON. All times are ISO-8601
UTC. All money values are USD floats. All probabilities are in `[0, 1]`.

OpenAPI tag groups: `meta`, `status`, `positions`, `trades`, `cycles`,
`markets`, `notes`.

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/agents` | `[{agent_id, display_name, trading_mode, last_cycle_ts, kill_switch}]` | Multi-agent index. MVP returns one row. |
| GET | `/agents/{id}/status` | `AgentStatus` (see schema below) | Hero band of dashboard. |
| GET | `/agents/{id}/equity-curve?range={1D,1W,1M,ALL}` | `[{ts, account_value, cash, unrealized, realized}]` | Mark-to-market on `market_snapshots.mid`. Aggregate cadence: 1D→1min, 1W→15min, 1M→1h, ALL→1d. |
| GET | `/agents/{id}/positions?status={open,closed,all}&limit=` | `Position[]` | Joined with `markets` and latest `market_snapshots`. |
| GET | `/agents/{id}/trades?status=&since=&until=&limit=` | `Trade[]` | From the table matching current mode. Each row carries `mode: 'paper'\|'real_capital'`. |
| GET | `/agents/{id}/cycles?limit=&offset=` | `CycleSummary[]` | Distinct `cycle_id` aggregated. |
| GET | `/agents/{id}/cycles/{cycle_id}` | `CycleDetail` | Full reasoning chain (see D.3). |
| GET | `/agents/{id}/markets/{market_id}` | `MarketDrilldown` | All predictions/decisions/trades/snapshots for one market. |
| GET | `/agents/{id}/notes?limit=` | `Note[]` | Recent agent notes. |
| GET | `/agents/{id}/lessons?status=` | `Lesson[]` | Default `status=open`. |

**Schema sketches** (write the full Pydantic models in `src/api/schemas.py`,
mirror naming with existing `src/shared/models/agent_io.py`):

```python
class AgentStatus(BaseModel):
    agent_id: str
    display_name: str
    trading_mode: Literal["paper", "real_capital"]
    kill_switch_active: bool
    last_cycle_id: str | None
    last_cycle_ts: datetime | None
    next_cycle_eta: datetime | None  # null if no cron metadata
    account_value_usd: float
    cash_available_usd: float
    cash_reserved_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    return_pct_since_inception: float
    sharpe_30d: float | None
    max_drawdown_pct: float
    n_trades_total: int
    win_rate_pct: float | None
    gross_exposure_ratio: float
    n_open_positions: int
    delta_24h_account_value_pct: float | None

class CycleSummary(BaseModel):
    cycle_id: str
    started_at: datetime
    finished_at: datetime | None
    n_decisions_trade: int
    n_decisions_skip: int
    n_decisions_hold: int
    n_trades_placed: int
    n_trades_filled: int
    total_cost_usd: float       # sum subagent_runs.cost_usd
    total_latency_ms: int
    n_web_searches: int
    had_errors: bool

class CycleDetail(BaseModel):
    summary: CycleSummary
    scanner: ScannerSection       # universe size, top-K markets list
    trading: TradingSection       # predictions[]
    risk: RiskSection             # decisions[] with full gate_results
    orders: list[Trade]           # trades placed in this cycle
    plan_written: CyclePlan       # the new plan synthesised
    plan_diff: PlanDiff           # added/removed vs. previous plan
    audit: AuditFooter            # cost, latency, raw envelopes
```

Pagination defaults: `limit=50` for trades, cycles, notes; cap at `500`.

---

## D. UI pages

### D.1 `/agents/[id]` — Dashboard (default landing)

This is the page the operator opens first thing in the morning.

**Hero band (always visible, sticky on scroll):**

- `ModeBadge`: pill showing `PAPER` (amber) or `REAL_CAPITAL` (red).
- Kill-switch indicator: green dot "armed" / red dot "TRIGGERED" + the reason
  if available from `system_state.kill_switch.value`.
- "Last cycle: 3 minutes ago" (relative) with absolute timestamp on hover.
  Greyed out if `>15min`, red if `>1h`.
- Next cycle ETA — only if cron metadata is available. Otherwise omit.

**KPI grid — 8 tiles, two rows of four:**

1. **Account Value** ($, large) + delta vs. 24h (color-coded).
2. **Cash** — split: available / reserved.
3. **PnL** — realized / unrealized split (two stacked numbers, color-coded).
4. **Return** % since inception.
5. **Sharpe** (30-day rolling).
6. **Max Drawdown** %.
7. **Trades** count + win-rate % subtitle.
8. **Gross Exposure** as ratio of equity (e.g. `42% of equity`).

**Equity curve** (full width, ~280px tall): account_value over time with a
PA-style range toggle `1D · 1W · 1M · ALL`. Three series:

- `account_value` — solid line, 1.5px, primary color.
- `cash` — thin line, 1px, muted.
- `realized_pnl` — step-line, 1px, accent.

Negative regions shaded faintly red. No fill gradient.

**Sections below the fold (this exact order, mirrors PA):**

1. **Active Positions** — table; columns: Market title (truncated, full on
   hover), Side (`YES`/`NO` pill), Size, Avg price, Current mid, Unrealized
   PnL ($, %), Age. Sort by unrealized PnL desc by default. Empty state:
   "No open positions."
2. **Recent Trades** — table; columns: Time, Market, Side, `qty @ price`,
   Notional, Fees, Status (pill: `filled`, `partial`, `open`, `cancelled`,
   `rejected`), Cycle (link). Default 20 rows. Mode pill on each row if
   mixed paper/real history exists.
3. **Recent Closed Trades** — same columns + Realized PnL highlighted
   (color-coded). Default 20 rows.
4. **Recent Settlements** — list combining (a) `pending_settlements` from
   the current `cycle_plan` and (b) markets where positions transitioned to
   `status=closed` recently. Each row: Market, Outcome, Realized PnL,
   Resolved at.
5. **Cycle Plan (current)** — collapsible card sourced from
   `cycle_plan WHERE superseded_at IS NULL`. Four sub-sections:
   `next_priorities`, `holds_with_rationale`, `opportunities_deferred`,
   `blockers`. Render JSONB content as a tidy list, not a JSON dump.

### D.2 `/agents/[id]/cycles` — Cycle index

Plain table; columns: Cycle ID, Started, Duration, Decisions
(`trade/skip/hold` colored pills), Trades placed, Trades filled, Cost ($
from `subagent_runs.cost_usd`), Web searches, Errors (red icon if any).
Click row → cycle detail.

Pagination: 50 per page, infinite scroll or numbered — implementer's choice.

### D.3 `/agents/[id]/cycles/[cycle_id]` — Reasoning chain

This page is the differentiator vs. PA — it is the operator's audit trail.
Layout is a vertical timeline with five large cards:

**1. Scanner-Reviewer card.**
Universe input size, filter parameters used (TTR window, liquidity floor),
top-K markets in a compact table: title, depth_1pct, spread, TTR (hours),
ambiguity score. Highlight markets that survived to the next stage.

**2. Trading-Agent card.**
Predictions table — one row per market the agent priced. Columns:
- Market (link to drill-down)
- `p_yes` (large, monospaced)
- `q_market` (from `inference_log.q_market`)
- `edge` (signed, color-coded green/red)
- Side intended (`YES`/`NO` pill)
- Thesis (one-sentence summary from `inference_log.thesis`, truncated;
  expand to show full `reasoning`)
- Web search icon (joined from `web_search_calls` via
  `subagent_runs.run_id`) — click to show the `web_search_calls` rows
  for this cycle/market
- Sources count — popover lists URLs from `inference_log.sources`

**3. Risk-Execution card.**
Decisions table — one row per market that reached risk. Columns:
- Market
- Action pill: `trade` (green), `skip` (grey), `hold` (amber)
- Gate timeline: six dots horizontal, in order
  `kill_switch → capital → solvency → sanity → concentration → cycle_cap`,
  green/red/grey based on `gate_results[gate].passed`. Hover reveals the
  per-gate reason.
- Proposed notional (from sizing) → Final clipped notional. If clipped,
  show as `$120 → $84` with the clipping gate name.
- Rationale (text)

**4. Orders placed card.**
Trades from this cycle (whichever table is current mode). Columns:
Market, Side, Size, Price, Notional, Fees, Status, Broker order ID
(monospaced, copyable). Failed orders shown in red with the error.

**5. Cycle plan written card.**
Renders the new `cycle_plan` for this `cycle_id` plus a diff vs. the
previous plan: priorities added, priorities resolved, holds added,
holds released, settlements newly pending, blockers added/cleared.

**Audit footer (collapsed by default):**
Total LLM cost (sum `subagent_runs.cost_usd`), total latency, web searches
count. "View raw envelopes" expands a `JsonViewer` showing each
`subagent_runs.envelope` for this cycle (debug aid).

### D.4 `/agents/[id]/markets/[market_id]` — Market drill-down

Cross-cutting view: everything the agent ever did or thought about *one*
market.

- **Header.** Title, category, end_date, status, resolution_source,
  ambiguity_score, external link to Polymarket
  (`https://polymarket.com/market/{slug}`).
- **Orderbook chart.** Best bid / ask / mid lines from
  `market_snapshots`, time on x-axis. Range toggle same as dashboard.
- **Prediction history.** Scatter or line of `p_yes` over time across all
  cycles, with thesis on hover, link to the cycle. Overlay `q_market`
  (the order-implied probability) from `inference_log.q_market` to make
  the edge visible.
- **Decisions table.** All decisions for this market across cycles,
  oldest→newest, with action and gate outcomes.
- **Trades table.** All fills, current open size, realized PnL summary.

### D.5 `/agents/[id]/notes` — Notes & Lessons

Two tabs:

- **Notes.** Table: body, tags (chips), last_accessed, created_at.
  Search box filters by text.
- **Lessons.** Table: observation, hypothesis, action_taken, outcome,
  status pill, created_at. Status filter (default `open`).

### D.6 `/agents` — Multi-agent index (multi-ready stub)

Required even at MVP because URL routing assumes it. Single row in MVP.
Columns mirror the PA homepage leaderboard:
**Agent · Mode · Account Value · PnL · Return % · Sharpe · Max Win · Max Loss · Trades · Last cycle**.

When the operator runs a second agent later, no UI changes are needed —
just a second row appears.

---

## E. Style guide (Prediction-Arena-near)

Design language: dark, quiet, finance-terminal. Not flashy. The grain
texture is the single most recognisable PA visual cue — keep it.

**Tailwind tokens** (write into `tailwind.config.ts`):

```ts
colors: {
  bg:        '#0a0a0a',  // page background
  surface:   '#141414',  // cards
  surface2:  '#1c1c1c',  // table row hover
  border:    '#262626',
  fg:        '#e5e5e5',
  muted:     '#9ca3af',
  positive:  '#4ade80',  // emerald-400 — gains, filled
  negative:  '#f87171',  // red-400 — losses, errors
  paper:     '#fbbf24',  // amber-400 — paper-mode badge
  real:      '#ef4444',  // red-500 — real-capital-mode badge
  accent:    '#a3a3a3',  // neutral accent for chart secondary lines
}
```

**Grain overlay.** Place `public/grain.png` (1024×1024 procedural noise) and
apply as a fixed full-viewport `::before` at `opacity: 0.05`,
`mix-blend-mode: overlay`. Critical to the PA feel.

**Typography.**

- Body: Inter (from `next/font/google`).
- Numbers, IDs, timestamps, code: JetBrains Mono.
- All money / probability / size values monospaced and right-aligned in
  tables.
- Headings: Inter, weight 500, no caps. Section titles small (14px) and
  uppercase-tracked.

**Density & layout.**

- Max content width 1280px, centered, side padding 24px.
- Vertical rhythm: 16px base, 32px between sections.
- Section dividers: a row of `- - - - -` in muted colour (PA does this) —
  not solid borders.
- Cards: 1px border, no shadows, no gradients, 12px radius.
- Tables: zebra-free, just hover row at `surface2`. Header row 12px
  uppercase tracked, muted.
- Pills: 1px border in the pill's colour, no fill (or 10% fill), monospaced
  text.

**Charts (recharts).**

- Lines 1.5px, no fill gradients.
- Axis labels in `muted`, ticks sparse.
- Tooltip card: `surface`, 1px border, monospaced numbers.
- Negative regions: faint `negative` fill at 8% opacity.

**Hard rules.**

- No emojis anywhere in UI text.
- No animated transitions on table rows (jitter on poll-refresh = bad).
- No skeleton shimmer; show "—" until loaded.

---

## F. Components (extract these)

Use shadcn/ui primitives where natural (`Card`, `Tabs`, `Tooltip`,
`Popover`, `Table`). Build custom on top:

| Component | Props (sketch) | Used in |
|---|---|---|
| `KpiTile` | `label, value, subValue?, delta?, format` | D.1 grid |
| `ModeBadge` | `mode: 'paper' \| 'real_capital'` | D.1 hero, D.6 |
| `EquityChart` | `series, range, onRangeChange` | D.1, D.4 |
| `RangeToggle` | `value: '1D'\|'1W'\|'1M'\|'ALL', onChange` | D.1, D.4 |
| `PositionsTable` | `positions, sort?` | D.1 |
| `TradesTable` | `trades, showRealized?, showMode?` | D.1, D.3, D.4 |
| `CyclePlanCard` | `plan, prevPlan?` (diff if prev given) | D.1, D.3 |
| `GateTimeline` | `gateResults, clippedNotional, proposedNotional` | D.3 |
| `PredictionRow` | `prediction, onMarketClick` | D.3 |
| `OrderbookSparkline` | `snapshots, height` | D.4 |
| `ThesisCell` | `thesis, fullReasoning, sources, webSearchCalled` | D.3 |
| `JsonViewer` | `data, collapsed?` | D.3 audit footer |
| `RelativeTime` | `iso, refreshSeconds=30` | many |
| `MoneyCell` | `value, currency='USD', signed?` | many tables |
| `ProbabilityCell` | `value` (0..1), `precision=3` | D.3, D.4 |

---

## G. Data freshness & caching

- **Dashboard (D.1) Server Components:** `revalidate: 5` (5 seconds).
  Acceptable load: cron cadence is minutes, the agent isn't producing
  state every second.
- **Cycle index (D.2):** `revalidate: 30`.
- **Cycle detail (D.3):** `revalidate: 3600` (immutable once written; the
  agent does not edit past cycles).
- **Market drill-down (D.4):** `revalidate: 30`.
- **Notes / lessons (D.5):** `revalidate: 60`.

Use `fetch(url, { next: { revalidate: N } })` from Server Components.
No client-side polling needed for v1. If the operator wants live updates,
that is a v2 (SSE on `/agents/{id}/events`).

---

## H. Hosting, auth, env

- Bind both services to `127.0.0.1`. Document this loudly in the README.
  An exposed dashboard leaks live strategy and could be scraped by
  counter-traders.
- No auth. If the operator ever needs remote access, they'll add an SSH
  tunnel or Tailscale + basic auth. Not in scope for MVP.
- Backend reads `DATABASE_URL`, `DEFAULT_AGENT_ID`, and the existing
  `TRADING_MODE` from the symlinked `.env`. No new secrets.
- Frontend reads `NEXT_PUBLIC_API_BASE` (default `http://127.0.0.1:8000`).
  No secrets in the Next.js bundle.

---

## I. Repo layout (where things go)

```
src/
  api/                   # NEW — FastAPI app
    __init__.py
    main.py              # FastAPI() + router includes
    deps.py              # DB session dependency (re-uses src/shared/adapters/db.py)
    schemas.py           # Pydantic response models
    routers/
      meta.py            # /agents
      status.py          # /agents/{id}/status, /equity-curve
      positions.py
      trades.py
      cycles.py          # cycle index + detail (the heaviest)
      markets.py
      notes.py           # notes + lessons
    services/            # query helpers (kept thin, no business logic)
      equity_curve.py    # mark-to-market reconstruction
      cycle_assembly.py  # joins for CycleDetail
  shared/                # UNCHANGED
  research/              # UNCHANGED
  execution/             # UNCHANGED
  risk/                  # UNTOUCHED — hard rule

apps/
  web/                   # NEW — Next.js 14 App Router
    package.json
    next.config.mjs
    tailwind.config.ts
    app/
      layout.tsx
      page.tsx           # redirects to /agents/<default>
      agents/
        page.tsx         # D.6 multi-agent index
        [id]/
          page.tsx       # D.1 dashboard
          cycles/
            page.tsx     # D.2
            [cycle_id]/page.tsx  # D.3
          markets/
            [market_id]/page.tsx  # D.4
          notes/page.tsx # D.5
    components/          # shadcn/ui base + custom (Section F)
    lib/
      api.ts             # typed fetch wrapper around FastAPI
      format.ts          # money / pct / probability formatters
      time.ts            # relative-time helper
    public/
      grain.png

tests/
  api/                   # FastAPI route tests
  fixtures/
    seed_demo.py         # seeds a paper-mode DB with fake cycle + trades for UI dev
```

---

## J. Verification (acceptance criteria)

The implementer is done when **all** of the following pass:

1. **Backend boots.** `uv run uvicorn src.api.main:app --port 8000` starts
   without errors. `http://127.0.0.1:8000/docs` shows the OpenAPI UI with
   the seven router groups present.
2. **Frontend boots.** `cd apps/web && npm install && npm run dev` starts
   Next.js on `:3000`.
3. **Demo data.** `uv run python tests/fixtures/seed_demo.py` populates a
   paper-mode DB with at least: 1 agent, 3 cycles, 8 markets, 12
   predictions, 12 decisions, 6 paper trades (mix of filled/cancelled),
   3 open positions, 2 closed positions, 1 current `cycle_plan`, 4 notes,
   2 lessons. The dashboard renders against this without empty-state
   placeholders.
4. **Equity curve.** All four ranges (1D, 1W, 1M, ALL) render without
   client errors and without rerunning the SQL on every range click
   (cache the underlying series, slice client-side).
5. **Click-through path** works end to end:
   `dashboard → cycle index → cycle detail → market drill-down → back`.
6. **Reasoning chain (D.3)** shows for at least one decision: the
   gate-by-gate timeline, the proposed→clipped notional change, the
   trading-agent's thesis, the sources from `inference_log`, and at least
   one `web_search_calls` entry rendered in the popover.
7. **Mode visibility.** When `TRADING_MODE=paper` in `.env`, the hero
   shows the amber `PAPER` pill. When set to `real_capital` (do not
   actually flip in test — just mock settings), it shows red `REAL_CAPITAL`.
8. **Kill-switch visibility.** With
   `INSERT INTO system_state(key, value) VALUES ('kill_switch', '{"active": true, "reason": "manual test"}')`,
   the hero shows the red triggered state and the reason.
9. **Static checks.** `uv run mypy --strict src/api` clean. `uv run ruff
   check src/api` clean. `cd apps/web && npm run lint && npm run
   typecheck` clean.
10. **No write SQL.** `grep -RE "(INSERT|UPDATE|DELETE)\\s" src/api/`
    returns nothing. Backend is provably read-only.
11. **`src/risk/**` untouched.** `git diff main -- src/risk/` is empty.
12. **Multi-agent stub works.** Hitting `/agents` shows a single-row
    table; the row links into `/agents/{default_id}`. Adding a second
    `agent_id` to settings does not require code changes to render a
    second row.
13. **Localhost-only.** Both services are bound to `127.0.0.1`, verified
    by `lsof -iTCP -sTCP:LISTEN | grep -E '8000|3000'` showing
    `127.0.0.1`, not `*` or `0.0.0.0`.

---

## K. Out-of-scope reminders

The implementer should explicitly **not** do:

- Touch anything under `src/risk/**` (Plan-Mode requirement, see
  `CLAUDE.md`).
- Add any code path that writes to the DB.
- Add knobs that flip `TRADING_MODE` from the UI.
- Introduce new numeric tunables outside `src/shared/config/settings.py`
  (Phase 3+ rule, see `CLAUDE.md`).
- Add a calibration / Brier / ECE chart — we do not collect the data.
- Add WebSocket or SSE infrastructure.
- Add Docker / k8s — local `uvicorn` and `npm run dev` is the deployment.
- Write a separate "data dictionary" doc — this file is it.

---

## L. Open questions for the implementer

If any of these block progress, ask the operator before guessing:

1. Is there a preferred package manager for the Next.js sub-tree (npm,
   pnpm, bun)?
2. Does the operator want an explicit "first cycle" zero-state design, or
   is the seed script enough to skip empty states entirely?
3. Should the cycle index page include cycles that produced **no**
   decisions (i.e. universe was empty)? Recommendation: yes, so the
   operator can see the agent ran.

Everything else: proceed on reasonable defaults.
