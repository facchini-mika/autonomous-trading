# Trading Feedback — MVP Outcome Ingestion + Tier-1 Roadmap

The Tier-1 evaluation block. Owns: ground-truth ingestion on resolved Polymarket markets — `outcome` and `realized_pnl` written onto existing rows, plus reconciliation against Polymarket-reported settlement values. **No code or prompt changes here** (Tier 2's job, `optimization.md`). **No lessons generation here either in MVP** — that lives in `optimization.md §1` as the daily lessons-summary script. This file is reduced to pure outcome math.

**What lives here:** resolution detection, realized-PnL math, mark-to-market convention, reconciliation rules, paper-mode promotion guidance. **What does not live here:** lessons / patterns / proposals (`optimization.md`), schemas (`data_infrastructure.md §1`), risk limits (`engineering.md §1`), runtime topology (`orchestration.md`).

---

## MVP scope (this version of the doc)

- **There is NO Trade Evaluation Team in MVP.** No `evaluator` agent, no subagents, no multi-agent runtime. Outcome ingestion is a deterministic Python script — no LLM, no Anthropic / OpenAI dependency.
- **One job: write ground truth on resolved markets.** `outcome`, `realized_pnl`, position-status closes. Idempotent on `(prediction_id, market_id)`.
- **Cadence: cron every 5–10 min** (central settings). Polymarket UMA-oracle resolution takes hours-to-days; sub-minute polling is wasted budget.
- **Lessons generation lives in `optimization.md §1`** — that file's daily script reads the rows this file writes, then emits `lessons` rows. Clean separation: this file = outcome math, that file = surprise heuristics + lessons.
- **Real_capital is supported from day 1** (`engineering.md §4`). The §2 Authority Boundary is therefore load-bearing in MVP — the script must NEVER touch live-trading credentials, signing keys, or order endpoints. It only writes ground-truth fields on existing rows.
- **Single trading agent in MVP** (`trading.md §2`) ⇒ no per-agent attribution, no `agent_performance` rolling stats, no pairwise-correlation tracking. Those live in §6 *Weiterer Ausbau*.
- **Everything else is post-MVP** — see §6.

---

## 1. MVP Outcome-Ingestion Script

Single deterministic Python script (location decided at implementation; e.g. `src/execution/outcome_ingestion.py`). Triggered by `cron` every 5–10 minutes.

**Inputs (read):**
- Polymarket Gamma API (`/markets` filtered by status transitions since the last successful run timestamp). Read-only access — no signing key, no order endpoints.
- `predictions` rows where `outcome IS NULL`.
- Open `positions` rows.
- Matching `decisions`, `trades`, and `paper_trades` rows for context.

**Outputs (write — ground truth only, on existing rows):**
- `predictions.outcome`, `predictions.realized_pnl` (set per resolved row).
- `trades.realized_pnl`, `trades.status='settled'` (and the equivalent on `paper_trades` when `TRADING_MODE=paper`).
- `positions` row marked closed; aggregate realized PnL recorded.
- A `last_outcome_ingestion_at` timestamp in `system_state` (`data_infrastructure.md §1`) so the next run knows the high-water mark.

**No other writes.** No `lessons`, no `agent_performance`, no `proposals`, no mutation of `predictions.p_raw` or `decisions.action` — those are immutable post-write.

**Pseudo-flow:**

```
1. Read system_state.last_outcome_ingestion_at (high-water mark).
2. Fetch Gamma /markets resolved since that timestamp.
3. For each resolved market_id:
     - SELECT predictions, trades/paper_trades, positions on this market_id.
     - Compute outcome (true/false), realized_pnl per row (§3 math).
     - UPDATE the rows in a single transaction (idempotent).
4. Reconcile against Polymarket-reported settlement values (§3).
5. Set system_state.last_outcome_ingestion_at = now.
6. Exit. Next cron tick repeats.
```

**Idempotency:** `UPDATE ... WHERE outcome IS NULL` ensures a re-run after a partial failure is safe. The `system_state` high-water mark prevents re-fetching the same window from Gamma.

Schemas: `data_infrastructure.md §1` (`predictions`, `trades`, `paper_trades`, `positions`, `system_state`).

---

## 2. Authority Boundary (load-bearing — real_capital flows)

Hard rules for the outcome-ingestion script:

- **NEVER** touch live-trading credentials, the EIP-712 signing key, or any `PolymarketAdapter` write surface. Read-only Gamma API access only.
- **NEVER** mutate `predictions.p_raw`, `predictions.inference_log`, `decisions.*`, or any non-ground-truth field. Allowed updates are limited to: `predictions.outcome`/`realized_pnl`, `trades.status`/`realized_pnl`, `paper_trades.status`/`realized_pnl`, `positions.status`/`positions.realized_pnl`/`positions.last_updated`, plus `system_state.last_outcome_ingestion_at`.
- **NEVER** insert `lessons`, `patterns`, or `proposals` rows. Those are owned by `optimization.md`.
- **NEVER** auto-correct disputed Polymarket settlements. A reconciliation diff > $0.50 (§3) flags the row for manual operator review; no auto-overwrite.

**Mechanical enforcement:**
- **Postgres role separation.** The outcome-ingestion script connects with a Postgres role that has `SELECT` on all trading tables and `UPDATE` **only on the ground-truth columns** listed above (column-level GRANT). `INSERT`/`DELETE` denied. Trading-cycle process and the lessons-summary script (`optimization.md §1`) use different roles. Phase-1-implementable: three Postgres roles + three `DATABASE_URL` values in `.env`.
- **No network egress** to anything other than Polymarket Gamma + local Postgres. Future post-MVP-egress allowlist (`engineering.md §13`) will codify this; MVP relies on the local-only deployment posture.

---

## 3. Result Computation (Math)

**Resolution detection.** Polymarket Gamma API is the source of truth, not the on-chain end-date timestamp. UMA optimistic oracle delays mean a market typically resolves 2–7 days after end. Treat Gamma's `resolved` status as authoritative.

**Outcome assignment.** For each `prediction` joined to a freshly-resolved `market_id`: set `outcome = true` if Gamma reports YES-resolution, `false` otherwise.

**Realized PnL per trade** (settled YES = $1; settled NO = $0):

```
trade_pnl = (settlement_price − fill_price) · size − fees − gas
```

`fees` and `gas` are recorded at fill time on the `trades` / `paper_trades` row; the script reads them, doesn't recompute.

**Prediction-level aggregation.** `predictions.realized_pnl` is the sum of `realized_pnl` across all `trades` and `paper_trades` whose `decision_id` shares the prediction's `(cycle_id, market_id)` join key. Predictions whose decision produced no trade (hold, skip, or gate-blocked) settle to `0.0`, not `NULL` — the row is "resolved with no trade", not "not yet resolved". The aggregation runs after the per-trade math below has populated the underlying `trades` / `paper_trades` rows.

**Position-level aggregation.** Sum `realized_pnl` across all entry trades on the same `market_id × side`. **Cost basis = weighted-average entry:**

```
new_avg = (old_qty · old_price + add_qty · add_price) / (old_qty + add_qty)
```

**Mark-to-market for open positions** (every script run, even when no markets resolve in the window): `unrealized_pnl = (current_best_bid − avg_entry_price) · size`. **Bid-based by design** — conservative (immediate-liquidation) valuation, never mid or ask.

**Reconciliation step.** Before writing, compare computed `realized_pnl` against Polymarket-reported settlement values for the same `market_id × side`. **Diff > $0.50 per trade** flags the row (a `reconciliation_flag` column or a row in `system_state` keyed by `recon_diff:<trade_id>`) for manual operator review. The script does **not** auto-overwrite — Polymarket may have rounding/timing quirks the script can't reason about.

---

## 4. Paper-Mode Promotion (informal guidance)

Before flipping `TRADING_MODE` from `paper` → `real_capital` (`engineering.md §4`), the operator should at minimum review:

- ≥ **30 days** continuous run in paper mode.
- Realized PnL, max paper drawdown, hit rate — judged against the operator's own thresholds (no hard gate).
- Spot-check of the lessons accumulated in the paper window (lessons live in `optimization.md §1`-driven table).

There is **no automated CI gate** that blocks the promotion — the operator owns the call, the audit log records it. Codifying it into a gate would invite gaming.

---

## 5. Failure Modes (MVP)

| Failure | Mitigation |
|---|---|
| Outcome-ingestion script crashes / cron miss | Non-fatal — next tick (5–10 min later) picks up via the `last_outcome_ingestion_at` high-water mark. Idempotent updates prevent duplicates. |
| Gamma API outage or 5xx | Script logs and exits cleanly; next tick retries. No state corruption since writes are transactional. |
| Reconciliation diff > $0.50 per trade | Row flagged; operator paged via standard structured-log alerting (`data_infrastructure.md §3`). The script proceeds with the rest of the batch but does not auto-overwrite the flagged row. |
| Postgres outage | Script fails fast on connection error; alerted via standard logs. Trading cycle separately keeps running (degraded — open positions can't be marked-closed until Postgres recovers). |

---

## 6. Weiterer Ausbau (post-MVP)

Deferred until MVP is stable. Each item is a future expansion of one of the §1–§5 sections; consistent with `engineering.md §13` and `optimization.md §5`.

### Trade Evaluation Team (multi-agent Tier-1)

The full Tier-1 architecture this file used to describe:

- **Single-member team:** `evaluator` agent, scheduled by cron / k8s `CronJob` every 1 minute (vs. the MVP's 5–10 min Python-script cadence).
- **Subagents** for fan-out:
  - `outcome-fetcher` — queries Gamma for resolved markets in the window.
  - `pnl-aggregator` — computes realized PnL per fill from `trades` rows.
  - `agent-performance-updater` — refreshes `agent_performance` rolling stats.
- **Permission mode:** standard. Hung prompts time out; next tick recovers cleanly.
- **Same authority boundary as MVP** (read-only Gamma, no signing keys, ground-truth-only writes), enforced by the Postgres role.

### Per-agent attribution

Once the trading roster has > 1 agent (`trading.md §8 Weiterer Ausbau`):

- `agent_performance(agent_id, time, hit_rate_30d, sharpe_30d, pnl_30d, n_samples)` table maintained by `agent-performance-updater`.
- `hit_rate_30d` = (# wins) / (# resolved predictions), where a "win" is a prediction whose `(p_raw − 0.5)` shares sign with `(outcome − 0.5)`.
- `pnl_30d` = sum of `realized_pnl` on decisions driven by this agent's prediction (proportional in multi-agent ensembles).
- Feeds the `meta-allocator` (`orchestration.md §3`) on its weekly rebalance.
- Pairwise-correlation tracking across agents — informational signal for `optimization.md §5` `strategy-optimizer` to consider retiring redundant agents when correlation drifts toward 1.0.

### Full Performance-Metric Table

| Metric | Definition | Target |
|---|---|---|
| Net PnL | Σ realized + Δ unrealized − fees − gas | > 0 monthly |
| Sharpe (annualized) | `mean(daily_ret) / std(daily_ret) · √252` | > 1.5 |
| Sortino | `mean / downside_std · √252` | > 2.0 |
| Max drawdown | peak-to-trough on equity curve | < 15% |
| Calmar | annual return / max drawdown | > 1.0 |
| Hit rate | wins / (wins + losses) | depends on edge dist. |
| Avg win / avg loss | | > 1.2 |

In MVP only **Net PnL > 0 monthly** is the reported gate (`trading.md §1`); the rest are tracked but not promotion gates.

### Strategy Update Loop (full table)

The semantic feedback loop in the architecture diagram. MVP form is trivial (outcomes → `optimization.md §1` lessons → next cycle's prompt critical-learning section). Post-MVP form has many channels:

| Artifact | Written by | Read by Trading Cycle |
|---|---|---|
| `predictions.outcome` + `realized_pnl` | This file (Tier 1) | `agent_performance` → `meta-allocator` weekly rebalance |
| `lesson` (raw observation) | Tier-2 evaluator (post-MVP) — in MVP, by `optimization.md §1` daily script | Surfaced via critical-learning section of per-cycle prompt |
| `pattern` (curated) | Tier-2 `pattern-miner` weekly | Top-K patterns injected into critical-learning section |
| `operating_doctrine` revision | Tier-2 `strategy-optimizer` proposes; operator merges | At every Trading-Cycle boot |
| `cycle_plan` | Trading-Cycle Lead at cycle close | Next cycle's Lead at boot (already MVP per `trading.md §2`) |
| `agent_performance` rebalance | `meta-allocator` weekly | Per-agent sizing caps consumed by `risk-engine` |

**Critical property** (MVP and post-MVP): the Strategy Update Loop never changes code or prompts directly — all semantic feedback flows through *data structures the Trading Cycle reads on its next boot*. Code/prompt changes go through Git PRs (`engineering.md §8` / `optimization.md`).

### Reporting Cadence

- Real-time Grafana dashboard.
- Daily PnL email.
- Weekly performance + per-agent attribution.
- Monthly deep-dive + strategy/roster review (the input to `optimization.md §5` monthly retrospective).

(MVP has none of this — structured JSON logs to stdout + Postgres queries when the operator wants them.)

### Reflexion / MAR cite-references

The Tier-1 lesson-emission pattern (when it returns post-MVP) follows Reflexion (Shinn et al., NeurIPS 2023) — natural-language reflections as episodic memory that future inference reads. Cross-agent critique that promotes lessons → patterns follows Multi-Agent Reflexion (MAR, 2025). The cross-agent critique role lives in `optimization.md §5` `meta-reviewer` post-MVP.

### Other deferred Tier-1 failure modes

- Trade Evaluation Team falls behind (resolutions not picked up for hours) — alert at queue-depth > N.
- Surprise threshold flags too many lessons (signal-to-noise drop) — `meta-reviewer` adjusts.
- Hung Anthropic API call freezes the team — relevant only when the Tier-1 team uses an LLM, which MVP does not.

---

## See also

- `optimization.md §1` — Tier-2 MVP daily lessons-summary script that reads what this file writes.
- `data_infrastructure.md §1` — schemas (`predictions`, `trades`, `paper_trades`, `positions`, `system_state`).
- `engineering.md §3` — capital gate.
- `engineering.md §4` — `TRADING_MODE` flag governance.
- `engineering.md §8` — branch protection / PR review counts (for any change to this file's script).
- `engineering.md §13` — `Weiterer Ausbau` master deferral list.
- `trading.md §2` — single-trading-agent context (why no per-agent attribution in MVP).
- `orchestration.md` — runtime topology (post-MVP team mechanics).
- `specs.md` — architecture diagram and entry point.
