# Trading Feedback — Tier 1 Evaluation + Strategy Update Loop

The Tier 1 evaluation block. Computes ground-truth results from resolved markets and generates natural-language `lessons` / `patterns` / `operating_doctrine` updates that flow back into Trading via prompt context. **No code changes here** — that is Tier 2's job (`optimization.md`).

This file owns: the **Trade Evaluation Team** (a single-member Claude Code Agent Team running every minute), the result-computation rules, the lesson-generation policy, the system-level evaluation metrics, the paper-mode promotion guidance, and the explicit description of the **Strategy Update Loop** that feeds back into agent prompts.

---

## 1. Purpose & Position in the Architecture

The Trading Team (`trading.md`) decides what to trade. The Trade Evaluation Team (this file, Tier 1) measures what happened. The Code Evaluation Team (`optimization.md`, Tier 2) decides what to change about the system based on what happened.

**The Trade Evaluation Team's job is exactly two things:**
1. **Compute ground truth on resolved markets** — outcome, realized PnL, per-agent hit-rate. Writes only to existing rows (`predictions.outcome`, `trades.realized_pnl`, `positions.status`, `agent_performance`).
2. **Emit natural-language observations** when something is worth noting — typically a market resolution that surprised the ensemble, an unusual fill, a notable PnL outlier. These are written as `lesson` rows with `source_agent_id='evaluator'` for the Code Evaluation Team to consume later.

**What the Trade Evaluation Team explicitly does NOT do:**
- Propose code, prompt, strategy, agent, or limit changes. (Tier 2.)
- Modify configuration. (Tier 2 + human review.)
- Touch the trading-team configuration or any live-trading endpoint. (Architectural separation.)
- Aggregate observations into patterns. (`pattern-miner` in Tier 2.)

This separation is deliberate. Tier 1 is **deterministic-with-agent-shell** — the work is mostly resolution lookup, PnL math, and threshold-flagging — and runs frequently (every minute). Tier 2 is **prescriptive** and runs in slower, deliberate batches. Mixing the two would couple a fast cron job to slow code-change reasoning, and would dilute the audit trail by interleaving "what happened" with "what to do about it."

**Position in the architecture (see `orchestration.md §1`):**

```
Trading Team           Trade Evaluation Team        Code Evaluation Team
(every 12 min)         (every 1 min — this file)    (daily / weekly)
   ↓                       ↓                            ↓
trades, predictions    →   outcomes, PnL, lessons   →   patterns, proposals, Git PRs
                                                            ↓
                                                       Operator review and merge
```

**Safety boundary (also in `engineering.md §2`):** read-only access to Polymarket Gamma API for resolution lookup; **no signing keys, no order endpoints, no CLOB write access**. Writes only ground-truth fields on existing rows in the long-term tables.

---

## 2. Trade Evaluation Team

A single-member team scheduled by cron / k8s `CronJob` every 1 minute. The Team Lead boots, the `evaluator` member runs, the team cleans up.

**Member:** `evaluator`. **Subagents:**
- `outcome-fetcher` — queries Polymarket Gamma API for resolved markets in the time-window since last run.
- `pnl-aggregator` — computes realized PnL per fill from `trades` rows tied to a now-resolved `market_id`.
- `agent-performance-updater` — refreshes `agent_performance.hit_rate_30d`, `pnl_30d`, `n_samples` based on the freshly-resolved set.

**Inputs (read):**
- `predictions` rows with `outcome IS NULL` whose `market_id` is in a freshly resolved state on Polymarket.
- Open `positions` rows whose `market_id` is now resolved.
- The matching `decisions` and `trades` rows for context.

**Outputs (write — ground truth only):**
- `predictions.outcome`, `predictions.pnl_realized` (set per resolved row).
- `trades.realized_pnl`, `trades.status` ("settled").
- `positions` row marked closed; aggregate realized PnL recorded.
- `agent_performance` row inserted (rolling 30d window, recomputed from the freshly-resolved set).
- A `lesson` row with `source_agent_id='evaluator'` if a surprise threshold is crossed (see §4).

**Permission mode:** standard (no `--dangerously-skip-permissions`). The team has read access to Polymarket Gamma API but no signing keys and no order-placement endpoints. A hung prompt halts the team — the next 1-min run picks up where it left off.

**Team-mechanic specifics** (Lead lifecycle, cleanup discipline, scheduler) follow the same pattern as the Trading Team — see `orchestration.md §2` "Off-cycle teams."

---

## 3. Result Computation

**Resolution detection.** Polymarket Gamma API is queried for markets that transitioned to `resolved` since the last successful run. UMA optimistic oracle delays mean a market typically resolves 2–7 days after the on-chain end date; the team treats the Gamma API as the source of truth, not the on-chain end-date timestamp.

**Outcome assignment.** Each `prediction` row's `market_id` is joined against the freshly-resolved set; on a match, the row's `outcome` field is set to the YES-resolution boolean (`true` / `false`).

**Realized PnL per trade** (settled YES at $1; settled NO at $0):

```
trade_pnl = (settlement_price - fill_price) * size - fees - gas
```

Aggregated to position-level realized PnL across all entry trades on the same `market_id` × `side`. Cost basis = weighted-average entry: `new_avg = (old_qty · old_price + add_qty · add_price) / (old_qty + add_qty)`.

**Per-agent hit-rate update.** For each agent that had ≥ 1 prediction on a now-resolved market in the rolling 30d window:
- `hit_rate_30d = (# wins) / (# resolved predictions)`. A "win" is a prediction where `(p_raw - 0.5)` and `(outcome - 0.5)` share sign — i.e. the agent leaned correct.
- `pnl_30d` = sum of `realized_pnl` on `decisions` driven by this agent's prediction (proportionally if multi-agent ensemble drove the trade).
- `n_samples` updated.

These feed the `meta-allocator` (`orchestration.md §3`) on its next 24h rebalance.

**Mark-to-market for open positions** (every minute, even when no markets resolve in the window): `unrealized_pnl = (current_best_bid - avg_entry_price) · size` for each open position. Conservative (bid-based) by design — see §5 "Valuation convention" below.

**Reconciliation step.** Before writing, the Trade Evaluation Team compares its computed realized PnL against Polymarket's reported settlement values for the same `market_id` × `side`. Diff > $0.50 per trade flags the row for manual operator review and pages — the deterministic `position-manager` reconciler (`data_infrastructure.md §1`) is the authoritative source on disputes; the evaluation team does not auto-overwrite.

---

## 4. Learning Generation

The Trade Evaluation Team writes `lessons` only when the resolution **surprised** the system relative to what was predicted. Default rule:

```
Write a lesson when: |p_consensus - outcome| > 0.3
                  OR realized fill slippage > 1% sustained over the trade
                  OR position closed at > 10% loss with active position-thesis belief
```

The lesson row contains:
- `source_agent_id = 'evaluator'`
- `trigger_event_id` — the resolved `market_id` or trade-id
- `observation` — descriptive: what happened (e.g. *"Israel x Lebanon Apr 26 resolved YES at $1.00; ensemble p_consensus was 0.62; 5 of 7 agents leaned YES. Largest dissent: red-team-adversary at 0.45."*)
- `hypothesis` — *blank* or minimal. Tier 1 does not theorize. If the operator wants pattern-level theories, those come from `pattern-miner` in Tier 2.
- `action_taken` — blank (Tier 1 takes no action).
- `outcome` — the realized outcome.
- `status = 'open'` (Tier 2 promotes / supersedes / closes).
- `tags` — auto-tagged with category, agent ids involved, surprise-magnitude bucket.

**Why this is intentionally minimal.** The Trade Evaluation Team writes raw observations because the system needs the surprise on record while the context is fresh — but interpretation, theorizing, and prescription happen on a slower deliberate cadence in Tier 2, where it's easier to review and easier to roll back. Mixing them would put speculative content into the audit trail of resolved-market outcomes.

**Pattern from prior art.** The natural-language-reflection mechanism follows the Reflexion design (Shinn et al., NeurIPS 2023) — store textual reflections as episodic memory that future inference reads. The cross-agent critique step that turns these `lessons` into `patterns` follows Multi-Agent Reflexion (MAR, 2025); the cross-agent critique role lives in `meta-reviewer` (`optimization.md §2`). Tier 1's job is the *write* side of the Reflexion loop; Tier 2's job is the *read-and-act* side.

**Promotion path** (lesson → pattern → proposal → PR):
1. Tier 1 writes a `lesson` (this section).
2. Tier 2's `pattern-miner` (weekly batch, `optimization.md §5`) clusters lessons into recurring `patterns`.
3. Tier 2's `strategy-optimizer` (exploit) or `strategy-explorer` (explore) reads patterns and drafts a `proposal` + Git PR.
4. Operator reviews and merges. Effective at next Trading-Team boot.

The Trade Evaluation Team is involved only in step 1.

---

## 5. Evaluation Metrics

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

**Per-agent attribution:** Capital ROI; Sharpe contribution; Hit rate; Pairwise correlation (low desired). Source: `agent_performance` table written by §3 above. Pairwise correlation is informational — when two agents drift toward 100% correlation, it's a signal for `optimization.md §2` `strategy-optimizer` to consider retiring one.

**Operational:** Cycle latency (P50/P95/P99); per-stage breakdown; fill rate; slippage realized vs expected (Δ over time); error rates per service. These are emitted by the live services (`data_infrastructure.md §1`), not computed by the Trade Evaluation Team.

**Reporting cadence:** Real-time Grafana; daily PnL email; weekly performance + attribution; monthly deep dive + strategy/roster review (the monthly review is the input to `optimization.md §5` monthly retrospective).

---

## 6. Strategy Update Loop

This is the **semantic feedback loop** in the user's architecture diagram — the arrow from Trading Feedback back into the Trading block via `Strategie / Agent-Aufbau / Daten`.

```
[Trading Team]  ──trades──►  [Trade Evaluation Team]  ──lessons──►  [Code Evaluation Team]
       ▲                                                                  │
       │                                                                  │ proposals + PR
       │                                                                  ▼
       │                                                          [Operator Review]
       │                                                                  │
       └────────────  next-cycle prompt context  ◄──────  merge  ─────────┘
```

**What flows back, and how:**

| Artifact | Written by | Read by Trading Team next cycle (where) |
|---|---|---|
| `predictions.outcome` + `realized_pnl` | Trade Eval Team (§3) | `agent_performance` informs `meta-allocator` weekly rebalance; per-agent prompts can include recent-resolution context |
| `lesson` (raw observation) | Trade Eval Team (§4) | NOT directly by Trading Team. First clustered into `patterns` by Tier 2, then surfaced via the **critical-learning section** of the per-cycle prompt (`trading.md §2`). |
| `pattern` (curated) | Tier 2 `pattern-miner` (weekly) | Yes — top-K relevant patterns injected into the critical-learning section by the Team Lead at boot |
| `operating_doctrine` revision | Tier 2 `strategy-optimizer` proposes; operator merges | Yes — at every Trading-Team boot, the Lead reads the active `operating_doctrine` row and propagates it to all members |
| `cycle_plan` (forward-looking handoff) | Trading-Team Lead at cycle close | Yes — next cycle's Lead reads the previous cycle's plan at boot |
| `agent_performance` rebalance | `meta-allocator` weekly | Yes — translates to per-agent sizing caps consumed by `risk-engine` |

**Critical property: this loop never changes code or prompts directly.** All semantic feedback flows through *data structures the Trading Team reads on its next boot* — no live mutation, no in-place patching. Code/prompt changes are a separate Tuning Loop owned by Tier 2 (`optimization.md §6`) that materializes as Git PRs.

This separation — **semantic feedback as data, code feedback as PRs** — is the architectural reason the Strategy Update Loop is fast (seconds-to-minutes) while the Tuning Loop is slow (days-to-weeks). Both are necessary; they operate at different cadences and through different channels.

---

## 7. Paper-Mode Promotion (informal guidance)

Before flipping `TRADING_MODE` from `paper` → `real_capital` (see `engineering.md §11`), the operator should review at minimum:

- ≥ **30 days** continuous run in paper mode
- Hit rate, realized PnL, max paper drawdown — judged against the operator's own thresholds (no hard gate)
- A spot-check of `lessons` accumulated in the paper window
- For a *new* strategy or trading agent: the explicit `kill-criterion` declared in the originating `proposal`, and confirmation that paper-mode behavior did not trigger it

There is **no automated CI gate** that blocks the promotion — the operator owns the call, the audit log records it. This is intentional: the operator's intuition matters here; codifying it into a gate would invite gaming.

**Distinct from the §3.1 anti-whipsaw rule** (`trading.md §3.1`): paper-mode is the *minimum before going live*; anti-whipsaw is the *minimum live duration before being replaced*. Both must hold for a strategy lifecycle:

```
new strategy proposal  →  ≥ 30d paper  →  flip to real_capital  →  ≥ 5–7d live before any replacement is eligible
```

Refinements to existing strategies via `strategy-optimizer` go through the same paper-mode minimum but are not blocked by anti-whipsaw.

---

## 8. Failure Modes — Tier 1

| Failure | Mitigation |
|---|---|
| Trade Evaluation Team falls behind (resolutions not picked up for hours) | Alert at queue-depth > N; operator can manually trigger a catch-up run; 1-min cron means natural recovery on next tick |
| Polymarket Gamma API returns inconsistent settlement data | Reconciliation diff > $0.50 per trade flags the row for manual review; Trade Eval Team does not auto-overwrite (`data_infrastructure.md §1` reconciler is authoritative) |
| Surprise threshold flags too many lessons (noise) | Threshold tunable in central settings; weekly `meta-reviewer` (Tier 2) reviews lesson volume and proposes adjustment if signal-to-noise drops |
| Surprise threshold flags too few lessons (missed signal) | Same path — `meta-reviewer` review; counter-rule for low lesson volume on weeks with significant PnL movement |
| Hung Anthropic API call freezes the team | Standard `--dangerously-skip-permissions` not used here; a hung prompt times out the run; next 1-min tick recovers cleanly. No state corruption because writes are idempotent on `prediction_id` and `market_id`. |

The Tier 1 team is the simpler of the two evaluation teams — it has narrow scope and idempotent writes, so most failure modes resolve themselves on the next scheduled run.

---

## See also

- `trading.md` — the trading runtime that produces what is evaluated here.
- `orchestration.md` — scheduler mechanics, off-cycle team patterns.
- `data_infrastructure.md` — schemas, observability.
- `engineering.md` — safety boundary specifics, central settings.
- `optimization.md` — Tier 2 evaluation, where lessons are turned into patterns, proposals, and Git PRs.
- `specs.md` — architecture diagram and entry point.
