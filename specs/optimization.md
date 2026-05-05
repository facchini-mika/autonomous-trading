# Optimization — MVP Self-Improvement Loop + Tier-2 Roadmap

How the system learns from outcomes and turns that learning into change. Owns the MVP self-improvement loop (a single deterministic daily script + manual operator review), the authority boundary that isolates self-improvement code from the live-trading path, and — under §5 *Weiterer Ausbau* — the full Tier-2 Code Evaluation Team architecture that comes post-MVP.

**What lives here:** the loop from outcomes back into prompts/code, plus the governance around it. **What does not live here:** how outcomes are measured (`trading_feedback.md`), schemas (`data_infrastructure.md`), risk limits (`engineering.md §1`), branch protection mechanics (`engineering.md §8`).

---

## MVP scope (this version of the doc)

- **There is NO Code Evaluation Team in MVP.** No multi-agent roster, no daily/weekly Claude-Code-Agent-Team batches, no automated proposal drafting, no automated PR generation, no explore-vs-exploit budget-rule arbitration.
- **Self-improvement = one daily Python script + manual operator review.** Deterministic, no LLM calls. Reads recent resolved predictions, emits structured `lessons` rows. The operator manually reads accumulated lessons and decides whether to change code, prompts, or limits — by hand, through normal Git PRs (per `engineering.md §8`).
- **The MVP supports real_capital from day 1** (`TRADING_MODE` may be flipped to `real_capital`, per `engineering.md §4`). That makes the §2 Authority Boundary load-bearing in MVP, not aspirational: a buggy self-improvement path could cost real money, so manual operator review is the only safety-defensible feedback channel for now.
- **Everything else is post-MVP** — see §5 *Weiterer Ausbau*. Consistent with `engineering.md §13 "Multi-team architecture"` and `engineering.md §13 "Strategy lifecycle governance"` deferrals.

---

## 1. MVP Self-Improvement Loop

```
[Trading Cycle]  ──predictions, decisions, paper_trades / real trades──►  [Postgres]
                                                                              │
[Trade Evaluation]  ──outcome, realized_pnl on resolved rows──►  [Postgres]   │
   (trading_feedback.md)                                                      │
                                                                              ▼
                                              [Daily Lessons-Summary Script]
                                              (deterministic Python, no LLM)
                                                          │
                                                          │ INSERT lessons row
                                                          ▼
                                                   [Postgres `lessons` table]
                                                          │
                                                          │ next cycle reads top-K
                                                          ▼
                                              [Trading Cycle agent prompt]
                                              (critical-learning section,
                                               trading.md §2)
                                                          │
                                                          │ operator periodically
                                                          ▼
                                          [Operator manual review of lessons]
                                                          │
                                                          │ decides → manual PR
                                                          ▼
                                          [engineering.md §8 branch protection]
                                                          │
                                                          ▼
                                           [Code/prompt/limit change merged]
                                                          │
                                                          ▼
                                                   [Next cycle uses it]
```

**Concrete MVP steps:**

1. **Outcome ingestion** is owned by `trading_feedback.md` — when a Polymarket market resolves, `outcome` and `realized_pnl` get written on the matching `predictions` and trade rows. Not this file's job.

2. **Daily lessons-summary script** (location decided at implementation; e.g. `src/research/skills/lessons_summary.py`):
   - Triggered by `cron` once per day.
   - Reads predictions resolved in the last 24h plus a small lookback window.
   - For each, applies a simple surprise heuristic (e.g. `|p_consensus − outcome_as_int| > 0.3` OR realized PnL outside the expected band by > X%).
   - Inserts a `lesson` row with `source_agent_id='daily_summary'`, the trigger event, a one-line observation, the realized outcome, and `status='open'`.
   - Idempotent on `(prediction_id, day)` so re-runs are safe.
   - **No LLM call**, no Anthropic / OpenAI dependency.

3. **Next-cycle prompt injection.** At the start of each Trading Cycle, the agent prompt's *critical-learning section* (`trading.md §2`) pulls the top-K most recent / most-relevant `lessons` rows (filtered by `status='open'` and recency). The agent reads them as additional context.

4. **Operator review.** The operator periodically (e.g. weekly) reviews accumulated lessons. If they suggest a code, prompt, or limit change is warranted, the operator drafts the PR by hand. The PR goes through `engineering.md §8` branch protection (≥ 1 reviewer; ≥ 2 humans on `src/risk/`). After merge, the next cycle uses the new code.

That is the full MVP self-improvement loop.

---

## 2. Authority Boundary (load-bearing in MVP — real money is in play)

**Hard rules** that apply to any code in the self-improvement path (today: just the daily lessons-summary script):

- **NEVER** modify `src/risk/` directly — `engineering.md §9` hook blocks AI edits; for the daily script, no write path to `src/risk/` exists by design.
- **NEVER** touch live-trading credentials, the EIP-712 signing key, or the `PolymarketAdapter` write surface.
- **NEVER** auto-merge, auto-push, or otherwise circumvent operator review.
- **NEVER** mutate `predictions`, `decisions`, `trades`, `paper_trades`, `positions`, or `cycle_plan` rows. The daily script's only DB write target is `INSERT INTO lessons`.

**Mechanical enforcement:**

- **Postgres role separation.** The daily script connects with the `lessons_summary` role that has `SELECT` on the trading tables and `INSERT` on `lessons`, and **no other privileges**. `UPDATE` and `DELETE` are denied at the role level. The trading-cycle process and the outcome-ingestion script use separate roles with their own write privileges (`orchestration.md §1`). Phase-1-implementable: three Postgres roles + three `DATABASE_URL` values in `.env`.
- **Branch protection** (`engineering.md §8`) is the merge gate. Nothing in the self-improvement path can self-merge.
- **Capital gate** (`engineering.md §3` / `src/risk/capital_gate.py`) is the runtime backstop. Even if lessons content somehow corrupted an agent prompt, the deterministic capital gate caps total deployment.

**Why this matters more in MVP than it would in a paper-only world.** With real capital flowing, a buggy "auto-improvement" path is a direct loss vector. MVP intentionally keeps the loop manual until enough operator-reviewed lessons have accumulated to justify automation — which is what the post-MVP roadmap in §5 builds on top.

---

## 3. Open-Source First / Prior-Art Reuse

The load-bearing principle is owned by `data_infrastructure.md §0`. It applies here too: before writing the daily script (or any post-MVP component), search GitHub / PyPI / papers for an existing project; build on it via thin wrappers; pin versions in `pyproject.toml`; document any roll-our-own decision as an ADR in `docs/adr/`.

For MVP: the daily script is small enough that there's likely no exact-fit OSS project — plain `psycopg` + simple Python suffices. The prompt-injection mechanism (top-K lessons → agent prompt) and the surprise heuristic are well-trodden patterns in the agent-reflection literature (Reflexion, Multi-Agent Reflexion); adopt the patterns, not unmaintained code.

---

## 4. Failure Modes (MVP)

| Failure | Mitigation |
|---|---|
| Daily-script outage (cron miss, exception, crash) | Non-fatal — next day's run picks up the missed window via the lookback. Idempotency prevents duplicate `lessons`. |
| Postgres outage | Daily script fails fast; trading cycle separately keeps running (degraded — no fresh lessons). Operator alerted via the standard structured-log alerting (`data_infrastructure.md §3`). |
| Lessons table grows unboundedly | Periodic manual sift by the operator; ad-hoc `DELETE WHERE status='archived'` or similar. Automated retention is post-MVP. |
| Operator overlooks important lessons | Symptom, not a system failure — MVP intentionally has no automated escalation. The operator is the sole reviewer; this is the price of "real money + safety margin." Post-MVP `meta-reviewer` (§5) is the eventual answer. |
| Prompt injection via malicious lesson content | Daily script's lesson text is composed from deterministic templates + DB-typed fields, not free-form LLM output, so the surface is small. As post-MVP agent-generated lessons come online (§5), prompt-injection hardening becomes its own task. |

---

## 5. Weiterer Ausbau (post-MVP)

Everything below is deferred until the MVP self-improvement loop has accumulated enough operator-reviewed lessons to justify automation. Each item is added when a measured limit forces it. Items grouped by area; consistent with `engineering.md §13`.

### Code Evaluation Team (Tier 2)

The full multi-agent system that automates what the operator does manually in MVP:

- **Roster (6 roles):**
  - `risk-auditor` — daily scan for risk-rule near-misses, anomalous fills, drawdown signals → writes `lessons`.
  - `pattern-miner` — weekly clustering of lessons into recurring `patterns`.
  - `strategy-optimizer` (**exploit**) — proposes deltas to existing strategies / prompts / sizing / agent-roster.
  - `strategy-explorer` (**explore**) — proposes entirely new strategies, new trading agents, new market categories.
  - `prior-art-scout` — enforces OSS-first / prior-art-reuse on every proposal; feeds candidates to `strategy-explorer`.
  - `meta-reviewer` — arbitrates exploit vs. explore, ranks proposals, posts top-K to the operator queue.
- **Runtime:** own Claude Code Agent Team (separate session, separate Lead, scheduled by cron / k8s `CronJob`), daily / weekly / monthly batches.
- **Per-batch authority boundary, separate Postgres role, no live-trading endpoint access.**
- Implementation flow: `proposal` row → feature branch (`code-eval/YYYY-MM-DD-<slug>`) → committed code change → `gh pr create` → operator merge gate.

### Explore + Exploit budget rule

- `meta-reviewer` enforces a fixed ratio when ranking weekly top-K (default K=5):
  - ≥ 1 explore proposal in top-K (when `strategy-explorer` produced any).
  - ≥ 2 exploit proposals in top-K (when `strategy-optimizer` produced any).
  - Remaining slots filled by `expected_pnl × confidence`.
- **Counter-rules:** drawdown > 10% from peak OR ≥ 3 losing weeks → exploit up-weighted; 5+ green weeks + low capacity utilization → explore up-weighted.
- All ratios live in central settings (`engineering.md §10`).
- **Decision-record:** each weekly batch closes with `meta-reviewer` writing `docs/code-eval/YYYY-WW.md` to the same feature branch.

### Multi-armed-bandit formalism (note)

The fixed `(min_explore=1, min_exploit=2, drawdown-tilt)` rule is a hand-engineered explore/exploit policy. The mathematically-grounded analogue is a non-stationary multi-armed bandit (Discounted UCB, Sliding-Window UCB, f-Discounted Sliding-Window Thompson Sampling). We deliberately keep the explicit ratio rule for operator interpretability and because reward attribution is too slow (weeks-to-months) for sophisticated MAB updates to pay off. Promotion to a formal MAB is on the roadmap if reward attribution becomes faster and the operator wants to delegate the ratio decision.

References: Vermorel & Mohri, ECML 2005; Garivier & Moulines, ALT 2011.

### Anti-Whipsaw Rule (≥ 5–7 days minimum live strategy lifetime)

A live strategy must run **≥ 5–7 days in `real_capital` mode** (default 5, central settings `STRATEGY_DISPLACEMENT_MIN_DAYS`) before `strategy-explorer` may propose a *replacement*. Refinements via `strategy-optimizer` are NOT blocked.

- **"Replacement"** = adding a new strategy that displaces ≥ 50% of an existing strategy's allocated capital, retiring a strategy entirely, or replacing the active `operating_doctrine` with a fundamentally different phased plan.
- **"Refinement"** = parameter tweaks, prompt rewrites of an existing trading agent, sizing deltas, `operating_doctrine` revision adjusting an existing phase. Not blocked.
- **Enforcement:** `meta-reviewer` checks the timestamp of the most recent strategy displacement; violations are deferred via `proposals.status='deferred'`. Operator override available via central-settings flag for emergencies.
- Coupled with the ≥ 30-day paper-mode minimum (`trading_feedback.md §4`), the full lifecycle becomes:
  ```
  proposal accepted  →  ≥ 30d paper mode  →  flip to real_capital  →  ≥ 5–7d live  →  eligible for replacement
  ```

### Resting / limit / TWAP order types

MVP submits every order with TIF=FAK (Fill-And-Kill / IOC) per `data_infrastructure.md §2` — the unfilled remainder is cancelled, no order rests on the book. Lifting this restriction (TIF=GTC for limit-on-book, GTD for time-bounded resting, FOK for atomic-fill) is post-MVP because it requires:
- open-order tracking in a new `open_orders` table or `positions` extension,
- locked-capital math in `solvency_gate.py` so reserved-but-unfilled notional is not double-spent,
- cross-cycle order-state in `cycle_plan` so the next cycle's Lead knows what's still resting,
- a long-running canceller for stale orders past their TIF window.

These are introduced together when the operator wants execution-strategy sophistication (TWAP, queue-priority laddering, post-only) on top of pure prediction quality. The PA baseline (FAK only) is the explicit MVP choice.

### Learning Loop Cadence

- **Daily:** `risk-auditor` scans last-24h trades; writes lessons for near-misses, outliers, slippage anomalies.
- **Weekly batch** (Monday morning, off-cycle):
  1. `pattern-miner` extracts patterns from the week's lessons.
  2. In parallel: `strategy-optimizer` and `strategy-explorer` each draft their proposals.
  3. `meta-reviewer` applies the budget rule, ranks, posts top-K to the operator, writes the decision-record.
- **Monthly retrospective:** archived lessons (> 90d) compressed into per-category LTKDs (Long-Term Knowledge Documents). Stale/contradicted points pruned by `meta-reviewer`. Also: monthly review of which explore-merged proposals actually improved live PnL.

### Full Tuning Loop (Tier-2 form)

```
[Trade Eval Team]  ──lessons, patterns──►  [Code Eval Team weekly batch]
                                                        │ proposals + Git PRs (with code changes)
                                                        ▼
                                              [Operator review and merge]
                                                        │ merge
                                                        ▼
                                              [main branch updated]
                                                        │
                                                        ▼
                                              [Next Trading-Team boot]
                                              reads updated team-spec, agent prompts, settings
```

- **Critical property:** never bypasses the operator. Every change flows through a Git PR.
- **Critical property:** touches code, not live state. Effective at the next Trading-Team boot — bounded blast radius (at most one cycle runs with a bad merge before the next tick can pick up a revert).

### Checks and Balances (post-MVP detailed table)

| Target of change | Required reviewers | Required tests/gates |
|---|---|---|
| Exploit: prompt rewrite (existing agent) | 1 agent reviewer + 1 human | ≥ 30d paper-mode (`trading_feedback.md §4`) |
| Exploit: strategy-parameter tweak | 1 agent reviewer + 1 human | ≥ 30d paper-mode |
| Exploit: retire trading agent (`agent_roster` remove) | `meta-reviewer` + 1 human | ≥ 60d underperformance + redundancy with another agent |
| Exploit: `operating_doctrine` revision | `meta-reviewer` + 1 human | Sanity-check on phase entry/exit; live-trial in paper if quantitative |
| Explore: new strategy | `meta-reviewer` + 2 humans | ≥ 30d paper + risk review + kill-criterion + anti-whipsaw window elapsed |
| Explore: new trading agent | `meta-reviewer` + 2 humans | ≥ 30d paper + persona / tool-allow-list review + pairwise correlation + kill-criterion |
| Code in `src/execution/` | `security-reviewer` + 2 humans | Tests + integration tests |
| Risk limit (`src/risk/`) | **2 humans only** — no agent override | Property-based tests pass |
| `MAX_CAPITAL_EUR` | 2 humans + audit-log entry | n/a |

Branch protection on `main` enforces these counts mechanically.

External validation: Robeyns et al., *A Self-Improving Coding Agent* (ICLR 2025 SSI-FM, arXiv:2504.15228) — same loop pattern, with operator-merge in place of auto-merge.

### Future Extensions

- **RL** (contingent on Anthropic managed fine-tuning for Opus): per-agent variants trained on resolved-prediction data with PnL-weighted reward.
- **Meta-learning / prompt optimization:** automated prompt search (DSPy / TextGrad) using historical PnL/hit-rate as objective.
- **Multi-model cloud ensemble:** add Claude-family models (Sonnet, Haiku) only if performance shows uncorrelated errors with Opus. Cloud-only rule (`trading.md §2`) is non-negotiable.
- **Cross-venue arbitrage:** Kalshi + sportsbook integration; statistical arb engine (adapter pattern already in place — `data_infrastructure.md §2`).
- **Real-time event-driven:** sub-second news → trade pipeline (lightweight classifier + dedicated agent).
- **Privacy/security maturation:** HSM-backed signing (production hardware); SOC2-style controls if scaling to external capital; ZK proofs for prediction provenance (research).
- **Scalar/categorical markets:** beyond binary; new aggregation logic.
- **Skill-library expansion** (`trading.md §8`): more procedural knowledge crystallized into named skills as patterns mature.
- **Formal MAB for explore/exploit ratio** (see above): replace the fixed rule with f-dsw Thompson Sampling once reward attribution becomes faster.

### Tier-2 Failure Modes

| Failure | Mitigation |
|---|---|
| Code-Evaluation agent overfits to recent noise | Require ≥ 90d data + significance threshold per proposal |
| `patterns` table bloats with low-value entries | Quarterly `meta-reviewer` prune; confidence-decay on stale patterns |
| Adversarial drift (proposes prompts that game metric) | All metrics validated on held-out forward window before promotion |
| Proposal queue grows into noise | `meta-reviewer` suppresses low-priority; operator sees top-K only |
| Code-Evaluation agents converge on bad direction | Mandatory human-in-the-loop; 2-human rule on src/risk/strategy/explore |
| `lessons` table self-contradicts | `status` field tracks supersession; `meta-reviewer` reconciles in monthly retro |
| Anthropic outage stalls weekly batch | Batch is non-realtime; defer to next cycle, no live impact |
| Code-Evaluation agent suggests bypassing `src/risk/` | `engineering.md §9` hook + `import-linter` prevent the diff existing |
| Whipsaw violations (rapid strategy churn) | Anti-whipsaw rule above; `meta-reviewer` defers offending proposals |
| Explore proposals with no kill-criterion slip through | PR template enforces kill-criterion field; `meta-reviewer` auto-rejects PRs missing it |
| Capital-allocation feedback rewards luck not skill | Adaptation-quality bonus capped at +10%; primary signal is rolling 30d hit rate + PnL with adequate `n_samples` |

---

## See also

- `trading.md` — the trading runtime that the loop targets; the *critical-learning section* of the per-cycle prompt is the read-side of the loop.
- `trading_feedback.md` — Tier 1 evaluation that writes `outcome` + `realized_pnl`; the upstream of the daily-lessons-summary script.
- `data_infrastructure.md` — `lessons` schema (§1), OSS-first principle (§0).
- `engineering.md` — central settings (§10), branch protection / PR review counts (§8), risk-layer protection (§3 + §9), `Weiterer Ausbau` master deferral list (§13).
- `orchestration.md` — runtime topology (post-MVP team mechanics).
- `specs.md` — architecture diagram and entry point.
