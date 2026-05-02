# Trading — The Executing Trading Instance

The runtime that decides what to trade and places orders. One block of the four-block architecture (see `specs.md`).

**What lives here:** the per-cycle Trading Team, the (single, MVP) trading agent, the strategy layer, the decision logic, and the per-trade gates as the trading layer sees them. **What does not live here:** runtime topology and team-mechanic details (`orchestration.md`), schemas + adapters + observability (`data_infrastructure.md`), risk-limit values + safety controls + repo conventions (`engineering.md`). Evaluation and learning loops are in `trading_feedback.md` (Tier 1) and `optimization.md` (Tier 2).

---

## MVP scope (this version of the doc)

- **Single strategy:** Mispricing — the only strategy in MVP.
- **Single trading agent** (no 7-persona ensemble, no aggregator, no disagreement metric).
- **Single research tool:** `web_search` (`src/research/skills/web_search.py`, OpenAI-backed). No `news_fetch`, no `historical_analogue_lookup`, no `related_market_scan` in MVP.
- **Agent Team runtime from day 1** — fresh Claude Code Agent Team per cycle (Lead + 3 members). The cycle is not a single Python script.
- **Minimal memory across cycles:** `notes` (per-agent LRU scratchpad) + `cycle_plan` (single active row, forward-looking handoff). No `beliefs`, no `operating_doctrine`, no position-thesis auto-flag in MVP.
- **Universe scoping:** top-K most-liquid Polymarket binary markets per cycle (default K=50, central settings). Not the unfiltered universe — one agent doing web research per market needs focus.
- **Real capital from day 1** is supported (`engineering.md §4`); paper is default.
- **Everything else is post-MVP** — see §8 *Weiterer Ausbau*.

---

## 1. Objective

Deploy capital on Polymarket binary prediction markets to generate risk-adjusted returns by **systematically identifying and exploiting probabilistic mispricings**: form an independent probability estimate `p_agent` via web research, compare against the market-implied probability `q = best_ask` (for buying YES) or `1 − best_bid` (for buying NO), trade when `|edge|` ≥ threshold.

**MVP KPI:** Net PnL > 0 monthly (post fees + gas). Sharpe / drawdown / hit-rate targets are tracked but not promotion gates in MVP. Out of scope: non-binary scalar markets, leverage beyond bankroll, market-making, externally-pooled capital.

---

## 2. Agent Team (MVP topology)

The Trading Team = one fresh Claude Code Agent Team per cycle (~12 min cadence; full team-mechanics in `orchestration.md`). Lead + 3 members; clean separation of responsibilities so each member's context window stays focused.

**Members (3):**

| Member | Role | Key inputs | Output artifact |
|---|---|---|---|
| `scanner-reviewer` | Pulls top-K liquid Polymarket markets and snapshots portfolio state. Combined to keep the team small. | Polymarket CLOB + Gamma; current Postgres positions/cash | `Universe` + `PortfolioState` |
| `trading-agent` | Mispricing analysis with `web_search`. Forms `p_agent` per market, returns `Prediction(p_yes, reasoning, edge)`. | `Universe`, `PortfolioState`, `notes`, top-K `lessons`, prev-cycle `cycle_plan`, `web_search` tool | `Prediction[]` |
| `risk-execution` | Applies risk gates from `src/risk/`, clips sizing, places order (paper OR real per `TRADING_MODE`). Combined because both deterministic. | `Prediction[]`, `PortfolioState`, `src/risk/` constants | `Decision[]` + `Trade[]` |

**Lead** — drives cycle clock, spawns members, persists artifacts, writes a fresh `cycle_plan` row at cycle close, calls `clean up the team` before exit. Never executes orders directly.

**Common contract.** Every member returns Pydantic-typed artifacts (`engineering.md §11`); the Lead validates via `TaskCompleted` hook (`engineering.md §9`) before downstream members consume.

**Model.** Anthropic Claude Opus exclusively (latest pinned per release). All inference cloud-only — no self-hosted, on-prem, or locally-run models in any phase. The OpenAI dependency is for the `web_search` *tool only*, not for model inference.

**Per-cycle prompt context for `trading-agent`** (assembled deterministically by the Lead from upstream artifacts; does not re-query state):
- Current timestamp + cycle id.
- Top-K market-data slices (orderbook, settlement criteria, depth at ±1% of mid).
- `PortfolioState` (cash, positions, unrealized + realized PnL, gross exposure, remaining capacity under per-trade gates from `engineering.md §1`).
- **Critical-learning section** — top-K recent `lessons` (status='open') from the daily lessons-summary script (`optimization.md §1`).
- **Previous cycle's `cycle_plan`** — priorities, holds-with-rationale, blockers (read-only).
- Agent's own most recent `notes`.
- Mispricing strategy doctrine (fixed text).
- `web_search` tool definition.
- Step-by-step trading protocol.

**Notes (per-agent scratchpad).** Single-agent in MVP, so just one `notes` table — no per-agent partitioning needed. Bounded LRU: max 50 × ~200 words. The `trading-agent` reads + writes via a `manage_notes` tool (read/write/edit). Schema: `data_infrastructure.md §1`.

**Cycle plan (forward-looking handoff).** Single-row portfolio-level artifact written at the very end of each cycle by the Lead, read by the *next* cycle's Lead at boot. Solves the gap that fresh-team-per-cycle creates: the next process knows nothing about what the previous one was about to do. Fields: `next_priorities`, `holds_with_rationale`, `pending_settlements`, `opportunities_deferred`, `blockers`. The Lead synthesizes the plan deterministically from in-flight artifacts (no LLM call needed). Always exactly one active row. Schema: `data_infrastructure.md §1`.

---

## 3. Strategy: Mispricing (the only one in MVP)

| Strategy | Trigger | Sizing | Notes |
|---|---|---|---|
| Mispricing | `|edge|` ≥ 3% (central settings) | Agent-proposed, clipped by per-trade gates | The only active strategy in MVP; receives 100% of allocated capital |

Sizing is the model's call within the hard limits in `src/risk/`. No Kelly formula, no smart routing.

---

## 4. Data Inputs per Cycle

What the `trading-agent` consumes per cycle (full source/schema details in `data_infrastructure.md §1`):

- **Universe snapshot** — top-K most-liquid Polymarket binary markets (orderbook, bid/ask, settlement rules). Filtered, not the full universe (cf. PA's "no filter" pattern, deferred).
- **Portfolio snapshot** — `PortfolioState` artifact built by `scanner-reviewer`.
- **Memory** — `notes` (LRU), previous cycle's `cycle_plan`, top-K recent `lessons` (from `optimization.md §1`).
- **Research tool** — `web_search` only (`src/research/skills/web_search.py`). The agent decides per market whether to invoke.

The agent does not directly read `market_snapshots` time-series, raw Polymarket orderbook streams, or `position-manager` state — those flow through the snapshot artifacts.

---

## 5. Trading Loop (cycle period 12 min)

Decision cycle (T = scheduler fire time). Four-stage spine (Prediction-Arena pattern): **Receive → Review → Analyze → Decide**. Boot first, cleanup last.

| Step | T | Phase | Action |
|---|---|---|---|
| 0 | 0–15s | Boot | Scheduler starts a fresh `claude` process (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, `--dangerously-skip-permissions`). `SessionStart` hook sweeps stale `~/.claude/teams/` directories. Lead loads the team-spec, spawns the 3 members, reads previous `cycle_plan` from Postgres. |
| 1 | +15s | Receive | `scanner-reviewer` fetches top-K liquid Polymarket markets → `Universe`. |
| 2 | +25s | Review | `scanner-reviewer` builds `PortfolioState`: cash, positions, unrealized + realized PnL. Mark-to-market vs current best bid (PA convention; `trading_feedback.md §5`). |
| 3 | +45s – +5m | Analyze | `trading-agent` runs inference per market (parallelism inside the agent's own session; `web_search` calls inline as needed). Output: `Prediction(p_yes, reasoning_blob, edge)` per scored market. Per-market timeout 60s. |
| 4 | +5m | Analyze | Edge computation per `Prediction`: `q = best_ask` (buying YES) / `1 − best_bid` (buying NO); `edge = p_yes − q`. Trade only if `|edge| ≥ 0.03`. |
| 5 | +6m | Decide | `risk-execution` applies the §6 gates in fixed order; clips sizing; rejects any trade that fails any gate. |
| 6 | +7m | Decide | `risk-execution` places orders. Marketable-limit, immediate execution. **Paper mode:** order written to `paper_trades`. **Real mode:** EIP-712-signed order to Polymarket CLOB (`data_infrastructure.md §2`). Internal idempotency key on every order. |
| 7 | +11m | Persist | Lead persists in-flight artifacts (`predictions`, `decisions`, `trades`/`paper_trades`) to long-term tables (`data_infrastructure.md §1`); writes any `notes` updates from the agent; **writes a fresh `cycle_plan` row** synthesized from the cycle's outputs. |
| 8 | +11m55s | Cleanup | Lead calls `clean up the team`; `Stop` hook asserts cleanup happened; process exits. Next cycle is a brand-new `claude` process at the next scheduler tick. |

The 12-min period is a central setting (`engineering.md §10`). A missed or failed cycle is a non-event — no orders placed, the next scheduled cycle runs cleanly.

---

## 6. Decision Logic + Per-Trade Gates (consumer view)

**Per agent:** raw output `p_yes ∈ (0, 1)`. No isotonic recalibration.
**Aggregation:** none in MVP (single agent).
**Edge & EV** (buying YES at ask `q`, $1 payoff): `edge = p_yes − q`; `EV per dollar = p_yes − q` (subtract `q · f_eff` for fees if material).
**Trade trigger:** `|edge| ≥ 0.03` (central setting).
**Sizing:** agent proposes notional. Risk-engine clips to the smallest of: concentration cap (15% of equity), solvency headroom, per-cycle-cap remaining.

The trading layer **consumes** the gates defined in `engineering.md §1`. Three gates, applied in fixed order before any order submission, plus the constitutional `MAX_CAPITAL_EUR` hard cap:

1. **Concentration** — proposed notional ≤ 15% of equity in any single market.
2. **Solvency** — cash ≥ proposed notional + estimated fees + open-order reservations.
3. **Per-cycle spending cap** — total notional opened this cycle ≤ cycle cap.

Limits are plain constants in `src/risk/`, never AI outputs. Direct CLOB calls bypassing `src/risk/` are forbidden by `import-linter` in CI (`engineering.md §3`).

---

## 7. Real-money safety (MVP)

Because real capital is in scope from day 1 (`engineering.md §4`), three boundaries matter beyond §6:

- **Paper as default.** A clean checkout / fresh deploy always boots in `paper`. Flipping `TRADING_MODE` to `real_capital` is a settings-file PR with ≥ 2 reviewer approvals.
- **Capital gate** (`engineering.md §3` / `src/risk/capital_gate.py`) — `MAX_CAPITAL_EUR: Final` rejects any order pushing gross deployed capital above the constitutional ceiling. The operator must set this constant before the first live cycle.
- **Kill switch** (`engineering.md §2`) — `system_state(key='kill_switch', value=true)` halts new orders. The `risk-execution` member reads this row before every order; existing positions stay open.

The §6 gates + capital gate + kill switch are the deterministic guard rails; the agent cannot trade through them by design.

---

## 8. Weiterer Ausbau (post-MVP)

Deferred until MVP is stable and a measured gap demands the addition. Each item is a future expansion of one of the §1–§7 sections; consistent with `engineering.md §13` and `optimization.md §5`.

**Multi-agent ensemble (extends §2).**
- 7-persona heterogeneous roster (`base-rate-bayesian`, `news-synthesizer`, `domain-router`, `historical-analogue`, `contrarian-skeptic`, `microstructure-reader`, `red-team-adversary`).
- Per-agent `notes` partitioning and per-agent `agent_state.allocation_pct` for the `meta-allocator`.
- Aggregator with consensus mean + disagreement std as observability signal.
- Pairwise-correlation tracking for retiring redundant agents.
- External validation: TradingAgents (Tauric Research, arXiv:2412.20138) — 7-role financial-LLM ensemble.

**Richer agent memory (extends §2).**
- `beliefs` — typed structured views per agent (`domain`, `scope`, `statement`, `p_estimate`, `confidence`, `evidence_uri`, revision lineage via `supersedes_id`).
- Position-thesis beliefs auto-flagged when market moves > 15% adverse to entry edge (closes the loop "why am I still holding this?").
- `operating_doctrine` — single active row with phased strategy + target date (`current_phase`, `phases.entry_condition` / `exit_condition` / `actions` / `forbidden`, `key_risks`); revision via `optimization.md §5` `strategy-optimizer`.

**More research tools (extends §4).**
- `news_fetch`, `historical_analogue_lookup`, `related_market_scan`. Agent calls inline during inference.
- Multi-source intake (NewsAPI / GDELT / X / Reddit / Tavily / Brave / FRED) per `data_infrastructure.md §1` post-MVP candidates.

**Strategy Skill Library (NEW §8 in original — Voyager pattern).**
- Named, executable, deterministic helpers (e.g. `compute_implied_distribution`, `detect_news_freshness`, `find_correlated_basket`).
- Promotion path: pattern → skill PR with unit tests + manifest entry → trading agent invokes via `call_skill(name, args)`.
- Versioned in `src/research/skills/index.toml`. Read-only consumers; agents cannot create or edit skills at runtime.
- External validation: Voyager (Wang et al., NeurIPS 2024).

**Multiple strategies + lifecycle governance.**
- `strategy-explorer` (in `optimization.md §5`) proposes new strategies.
- Per-strategy PnL tracking; capital allocation per strategy.
- Anti-whipsaw rule (`optimization.md §5`): ≥ 5–7d real_capital lifetime before strategy displacement.
- Paper-mode promotion gate (`trading_feedback.md §4`): ≥ 30d in paper before flipping to real_capital for any new strategy.

**Multi-cadence loops (extends §5).**
- 1s snapshot loop for active-position orderbooks.
- 30s reconciler matching internal state to broker truth (`data_infrastructure.md §2`).
- 24h capital-allocation rebalance (`meta-allocator`, `orchestration.md §3`).
- Hourly / daily / weekly Code-Evaluation batches (`optimization.md §5`).

**PA-style "no-filter" universe (extends §4).**
- Drop the top-K filter; let the agent ensemble triage the full Polymarket universe itself during inference. Requires the multi-agent ensemble (parallelism) to be feasible per cycle.

**Recent-settlement / recent-trade prompt windows (extends §2).**
- Inject last-N resolved markets with realized PnL and last-N closed trades with realized PnL into the per-cycle prompt context.

---

## See also

- `orchestration.md` — runtime topology, fresh-team-per-cycle mechanics, capital allocation (post-MVP elaborations).
- `data_infrastructure.md` — schemas, prediction-market adapter, MVP single-tier storage, OpenAI web-search intake.
- `engineering.md` — risk-limit values, safety controls, central settings, repo conventions, hooks (§9), `Weiterer Ausbau` master deferral list (§13).
- `trading_feedback.md` — Tier 1 evaluation that writes outcomes; the upstream of the lessons that flow into this file's prompt context.
- `optimization.md` — Tier 2 self-improvement loop (MVP: daily lessons-summary script + manual operator review).
- `specs.md` — architecture diagram and entry point.
