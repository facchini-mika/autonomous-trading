# Optimization — Tier 2 Code Evaluation + Tuning Loop

The Tier 2 evaluation block. **Single objective: maximize the system's net PnL over time.** Reads the outputs of Trading (`trading.md`) and Trade Evaluation (`trading_feedback.md`), proposes code/prompt/strategy/agent-roster changes, opens Git PRs for the operator to review and merge. Never self-merges, never touches live-trading endpoints.

This file owns: the **Code Evaluation Team** roster, the explore-vs-exploit budget rule, the anti-whipsaw rule, the cadence of daily/weekly/monthly batches, the review counts that gate every kind of change, the prior-art reuse policy, the future-extensions roadmap, and the Tier 2 failure modes. It also documents the **Tuning Loop** that produces the Git PRs the operator merges.

---

## 1. Purpose & Authority Boundary

The Code Evaluation Team has **one objective: maximize the system's net PnL over time.** It pursues this on two parallel tracks every batch:

- **Exploit** (`strategy-optimizer`) — refine the existing strategies, prompts, sizing, agent roster. Make what works work better.
- **Explore** (`strategy-explorer`) — propose entirely new strategies, new trading agents, new market categories that are not yet in the system. Find new sources of edge.

The team then **arbitrates between exploit and explore proposals** before passing the top-K queue to the operator (§3 budget rule).

**Authority and limits:**

| Capability | Allowed | Not allowed |
|---|---|---|
| Read `predictions`, `decisions`, `trades`, `positions`, `agent_performance`, `lessons`, `notes`, `beliefs`, `cycle_plan`, `operating_doctrine` | Yes | — |
| Write `lessons`, `patterns`, `proposals` | Yes | — |
| Open Git PRs on feature branches | Yes | — |
| Self-merge a PR | — | Forbidden, mechanically enforced by branch protection (`infrastructure.md §8.3`) |
| Modify live-trading state in any in-process way | — | Forbidden — no credentials for production CLOB endpoints |
| Edit `risk/` directly | — | Forbidden — `infrastructure.md §8.4` hook blocks it |
| Bypass the §7 review counts | — | Forbidden — branch protection on `main` enforces them |
| Change `MAX_CAPITAL_EUR` (`infrastructure.md §8.8`) | Propose only | Two-human approval required to merge |

The PR is the contract. Code change + rationale + paper-validation reference (when quantitative) lands in the operator's review queue. The operator is the merge gate.

---

## 2. Roster

| Agent | Role | Trigger | Output |
|---|---|---|---|
| `risk-auditor` | Scan recent trades for risk-rule near-misses, anomalous fills, drawdown signals | Daily + on alert | `lessons` row |
| `pattern-miner` | Cluster lessons into recurring patterns | Weekly | `patterns` row + supersedence links |
| `strategy-optimizer` (**exploit**) | Make what works work better. Read agent_performance + losing trades + new patterns; propose **deltas to existing strategies / prompts / sizing / agent-roster** (parameter tuning, prompt rewrites, retiring underperforming agents, revising `operating_doctrine` — see `trading.md §2`). Each proposal cites the existing baseline it improves on. | Weekly + on doctrine-phase transition + after a drawdown alert | `proposals` row + draft Git PR for operator review |
| `strategy-explorer` (**explore**) | Find new sources of edge. Survey resolved markets, missed opportunities, market types we don't trade yet, and §8 prior-art; propose **entirely new strategies, new trading agents (with full persona + prompt + tool allow-list), or new market categories** that are not currently in the system. Each proposal must include a paper-mode test plan and a kill-criterion (when to abandon). | Weekly | `proposals` row + draft Git PR for operator review |
| `prior-art-scout` | Enforce §8 — search GitHub/PyPI/papers before custom builds; **proactively feeds candidate ideas to `strategy-explorer`**. | On every new-component PR open + weekly explore-batch | Prior-art note posted to PR + idea-list to `strategy-explorer` |
| `meta-reviewer` | **Arbitrate exploit vs. explore.** Aggregate, dedupe, prioritize all proposals from both tracks; rank by expected PnL impact × confidence × paper-test feasibility; apply the explore-budget rule (§3) so neither track starves the other; route top-K queue to operator. | Weekly | Decision queue (Slack/email) — the operator's review queue, with each item labeled **`[exploit]`** or **`[explore]`** |

**Team structure.** Code-evaluation agents form their own **Code Evaluation Team** — a *separate* Claude Code Agent Team session, distinct from both the Trading Team (`trading.md`, `infrastructure.md §8.22`) and the Trade Evaluation Team (`trading_feedback.md §2`). The "one team per session" Cloud-doc constraint is respected because each Code-Evaluation batch runs in **its own Claude Code process**, on schedule, never simultaneously with another team's session.

- A dedicated **Code-Evaluation Team Lead** orchestrates each scheduled batch (daily, weekly, monthly per §5). The Lead session is started by cron / scheduler (`schedule` skill or k8s `CronJob`), runs the batch, and cleans up (`clean up the team` per Cloud-doc) before exiting.
- Members above are spawned per batch and tear down with the team at end of run; their short-term coordination uses the standard task-list + mailbox primitives.
- `pattern-miner`, `strategy-optimizer`, and `strategy-explorer` may spawn subagents for fan-out (one subagent per lesson cluster, per existing strategy under review, per candidate new-strategy idea, per category being analyzed).
- The Code Evaluation Team is **isolated from both the Trading Team and the Trade Evaluation Team** — separate Lead, separate task list, no shared mailbox. The only coupling is the long-term memory layer: Code-Evaluation agents *read* episodic + reflective + agent-performance tables and *write* `lessons` / `patterns` / `proposals` / Git PRs.
- Permission mode: standard (no `--dangerously-skip-permissions`). The Code Evaluation Team has no live-trading endpoint access by design (`infrastructure.md §6` safety boundary), so blocking on permission prompts for unexpected tool use is acceptable behavior. If a batch hangs on a prompt, the scheduler kills it after a timeout and pages the operator.

**Implementation flow for a code change.** When either `strategy-optimizer` (exploit) or `strategy-explorer` (explore) proposes a non-trivial change (parameter tweak, prompt rewrite, sizing delta, new strategy, new trading agent), it:
1. Writes the `proposals` row with rationale and supporting `lessons` references.
2. Creates a feature branch (`code-eval/YYYY-MM-DD-<short-slug>`) and commits the actual code change — new files for a new agent (`research/agents/<id>.py` + system prompt + `.claude/agents/<id>.md`), edited files for a tweak.
3. If the change is quantitative (sizing, threshold, new strategy), drafts a paper-mode test plan as part of the PR description.
4. Opens the PR via `gh pr create`, assigns the operator as reviewer, sets `meta-reviewer` as the agent reviewer.
5. **Stops there.** No self-merge, no auto-deploy. The operator reviews and decides.

**Long-term memory tables** that this team writes to:

```sql
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

(Schemas owned by `infrastructure.md §3`.)

- `lessons` append-only; supersedence via `status`, never deletion. Replay always possible.
- `patterns` curated by `pattern-miner`; references the lessons that built it.
- `proposals` drives the change pipeline (§7).
- A `proposal` with `target_kind='operating_doctrine'` and an accepted `decided_by` writes a new `operating_doctrine` row (`trading.md §2`) and supersedes the prior active row. The trading layer reads the new doctrine on the next cycle's boot.
- A `proposal` with `target_kind='agent_roster'` proposes adding a new trading agent to the `trading.md §2` ensemble (with full prompt + tool allow-list + persona definition in the PR), or retiring an existing one. Merge updates `.claude/agents/` and `.claude/teams/trading-team.spec.json`; effective at the next Trading-Team boot.

All members are defined as Claude Code subagent definitions (`infrastructure.md §8.5`), reusable as both delegated subagents and Code-Evaluation-Team teammates.

---

## 3. Explore vs. Exploit — Budget Rule

The Code Evaluation Team must run **both tracks every batch** so the operator's review queue always contains a mix of refinements and new bets. `meta-reviewer` enforces this with a simple budget when ranking the weekly top-K (default K=5):

- **At least 1 explore proposal** in the top-K (when `strategy-explorer` produced any).
- **At least 2 exploit proposals** in the top-K (when `strategy-optimizer` produced any).
- Remaining slots filled by raw expected-PnL × confidence ranking across both tracks.
- Counter-rule: if the live system is in active drawdown (>10% from peak per `infrastructure.md §5` alerts) **or** has had ≥ 3 consecutive losing weeks, exploit is up-weighted — the queue tilts toward stabilizing what exists before chasing new edges. Inverse case (5+ green weeks, low capacity utilization): explore up-weighted.

These ratios are tunables in the central settings file (`infrastructure.md §8.21`). The point is that the team is **structurally biased to do both**, not to drift into one mode and stay there.

**Decision-record.** Each weekly batch closes with `meta-reviewer` writing a short markdown decision-record in `docs/code-eval/YYYY-WW.md` to the same feature branch as the proposals: how many proposals each track produced, how many made the top-K, why, and the explore/exploit weights applied. Audit trail for the operator.

### 3.1 Multi-armed-bandit formalism (note)

The fixed `(min_explore=1, min_exploit=2, drawdown-tilt)` rule above is a hand-engineered explore/exploit policy. The mathematically-grounded analogue is a **non-stationary multi-armed bandit**: treat `{exploit, explore}` as two arms, observe the realized reward per merged proposal (PnL change attributable, measured over a forward window), and select the next batch's arm allocation via:

- **Discounted UCB** (Garivier & Moulines, 2011) — discount past rewards exponentially so old data fades.
- **Sliding-Window UCB** — only consider rewards within a fixed window.
- **f-Discounted Sliding-Window Thompson Sampling (f-dsw TS)** — Bayesian variant explicitly designed for non-stationary, concept-drifting environments.

We **deliberately keep the explicit ratio rule** rather than promote to a formal MAB. Reasons: (a) operator interpretability — a fixed ratio is auditable at a glance; (b) the reward signal (PnL attribution per merged proposal) is delayed by weeks-to-months, so the arm-selection cadence is too slow to benefit from sophisticated MAB updates; (c) the counter-rules (drawdown tilt, sustained-outperformance tilt) already encode the most important non-stationary heuristic. The MAB formalism is the right fit if/when (a) reward attribution becomes faster and (b) the operator wants to delegate the ratio decision to the system. Until then, the citation is here as a roadmap pointer.

Standard reference: Vermorel & Mohri, *Multi-armed bandit algorithms and empirical evaluation* (ECML 2005); Garivier & Moulines, *On Upper-Confidence Bound Policies for Switching Bandit Problems* (ALT 2011).

---

## 4. Anti-Whipsaw Rule (5–7 day strategy lifetime minimum)

A live strategy must run **≥ 5–7 days in `real_capital` mode** (default 5, central settings `STRATEGY_DISPLACEMENT_MIN_DAYS`) before `strategy-explorer` may propose a *replacement* strategy. **Refinements via `strategy-optimizer`** are NOT blocked.

**What "replacement" means:**
- Adding a new strategy that displaces an existing one in capital allocation (≥ 50% of the existing strategy's allocated capital).
- Retiring a strategy entirely (`agent_roster` remove on a strategy-aligned agent).
- Replacing the active `operating_doctrine` with a fundamentally different phased plan.

**What "refinement" means** (not blocked by this rule):
- Parameter tweaks on an existing strategy (e.g. edge threshold from 3% → 2.5%).
- Prompt rewrites of an existing trading agent.
- Sizing deltas within an existing strategy.
- Adding an explanatory `lesson`-derived constraint to an agent's prompt.
- `operating_doctrine` revision that adjusts an existing phase rather than replacing the structure.

**Why the rule exists.** Without it, `strategy-explorer` could propose a new strategy every week, the operator could merge it, and the system would whipsaw between strategies before any of them accumulated enough live PnL data to evaluate. Five to seven days of `real_capital` running is the minimum required for the operator to form a defensible opinion on whether a strategy is delivering edge or noise. Coupled with the ≥ 30-day paper-mode minimum (`trading_feedback.md §7`), the full lifecycle becomes:

```
proposal accepted  →  ≥ 30d paper mode  →  flip to real_capital  →  ≥ 5–7d live  →  eligible for replacement
```

**Enforcement mechanics.** `meta-reviewer` checks the timestamp of the most recent strategy displacement before accepting an `[explore]` proposal into the top-K. If the constraint is violated, the proposal is held in `proposals.status='deferred'` until the window has elapsed; an info-level alert tells the operator a deferral happened so they can override if there's a good reason (e.g. obvious fundamental break in the live strategy that warrants emergency replacement — the operator can flip the override flag in central settings).

---

## 5. Learning Loop Cadence

- **Daily**: `risk-auditor` scans last-24h trades; writes lessons for near-misses, outliers, slippage anomalies.
- **Weekly batch** (Mon morning, off-cycle) — runs both tracks in parallel, then arbitrates:
  1. `pattern-miner` extracts patterns from the week's lessons.
  2. **In parallel:**
     - `strategy-optimizer` (**exploit**) generates proposals from new patterns + losing trades + agent_performance drift on existing strategies and agents.
     - `strategy-explorer` (**explore**) surveys missed-edge markets + prior-art (with `prior-art-scout`) and proposes new strategies / new trading agents that don't exist yet.
  3. `meta-reviewer` applies the §3 explore/exploit budget rule, ranks the combined proposal pool by expected PnL × confidence × paper-test feasibility, posts the top-K queue to the operator, and writes the weekly decision-record (§3).
- **Monthly retrospective**: archived lessons (> 90d) compressed into per-category **LTKDs** (Long-Term Knowledge Documents) loaded as background for Code-Evaluation agents going forward. Stale/contradicted points pruned by `meta-reviewer`. Also: monthly review of which explore proposals merged in the last 90 days actually improved live PnL — feeds back into how aggressively `strategy-explorer` should propose vs. how cautiously `strategy-optimizer` should refine.

This compaction is essential — without it the lessons store grows unboundedly.

---

## 6. Tuning Loop

This is the **technical feedback loop** in the user's architecture diagram — the arrow from Optimization back into Trading via code changes (the "Tuning Loop"), distinct from the semantic Strategy Update Loop in `trading_feedback.md §6`.

```
[Trade Evaluation Team]
    ↓ (lessons, patterns)
[Code Evaluation Team weekly batch]
    ↓ proposals + Git PRs (with actual code changes committed)
[Operator review and merge]
    ↓ merge
[Repository main updated with new code / prompts / agent roster]
    ↓
[Next Trading Team boot at the cron tick after merge]
    ↓ reads updated team-spec, agent-prompts, settings, operating_doctrine
[Trading Team uses the new code]
```

**What flows through the Tuning Loop, that the Strategy Update Loop does NOT:**

| Change kind | Channel | Latency |
|---|---|---|
| Agent prompt rewrite | Tuning Loop (PR + merge) | days–weeks |
| New trading agent added to ensemble | Tuning Loop (PR + merge, `.claude/agents/` + team spec) | days–weeks |
| Existing strategy parameter tweak | Tuning Loop (PR + merge, central settings) | days–weeks |
| New strategy added | Tuning Loop (PR + merge + ≥ 30d paper) | weeks |
| Skill added to library (`trading.md §8`) | Tuning Loop (PR + merge) | days–weeks |
| `operating_doctrine` revision | Tuning Loop (proposal merge writes new active row) | days |
| Risk-limit value change (`infrastructure.md §2`) | Tuning Loop with §7 stricter review counts | days–weeks |

**Critical property: the Tuning Loop never bypasses the operator.** Every change flows through a Git PR. The operator's review queue is the system's commit log of "what the autonomous team thinks should change."

**Critical property: the Tuning Loop touches code, not live state.** Even after merge, the change does not affect the running cycle — it takes effect at the *next* Trading Team boot, which is the next cron tick after merge (max 12 minutes later). This means a bad merge has a bounded blast radius: at most one cycle runs with the bad change before the operator can revert and the next cron tick picks up the revert.

---

## 7. Checks and Balances

Every Code-Evaluation-agent change goes through the standard merge pipeline. **No Code-Evaluation agent may self-merge**; none may write to `risk/` (`infrastructure.md §8.4` hook). The PR is the contract: code change + rationale + paper-validation reference (when quantitative). The operator is the merge gate.

| Target of change | Required reviewers | Required tests/gates |
|---|---|---|
| **Exploit:** prompt rewrite (existing agent) | 1 agent reviewer + 1 human | ≥ 30d in paper mode (`trading_feedback.md §7`) |
| **Exploit:** strategy-parameter tweak | 1 agent reviewer + 1 human | ≥ 30d in paper mode |
| **Exploit:** retire trading agent (`agent_roster` remove) | `meta-reviewer` + 1 human | Justified by ≥ 60d underperformance vs. ensemble + redundancy with another agent |
| **Exploit:** `operating_doctrine` revision (`trading.md §2`) | `meta-reviewer` + 1 human | Sanity-check on phase entry/exit conditions; live-trial in paper if quantitative |
| **Explore:** new strategy | `meta-reviewer` + 2 humans | ≥ 30d in paper mode + risk review + explicit kill-criterion + §4 anti-whipsaw window elapsed since last displacement |
| **Explore:** new trading agent (`agent_roster` add) | `meta-reviewer` + 2 humans | ≥ 30d in paper mode + persona / tool-allow-list review; pairwise correlation against existing roster measured; explicit kill-criterion |
| Code in `execution/` | `security-reviewer` + 2 humans | Tests + integration tests |
| Risk limit (`risk/`) | **2 humans only** — no agent override | Property-based tests pass |
| Hard cap `MAX_CAPITAL_EUR` | 2 humans + audit-log entry (`infrastructure.md §8.8`) | n/a |

Branch protection on `main` enforces these counts mechanically.

**External validation of the autonomous-PR-with-human-review pattern.** Robeyns et al., *A Self-Improving Coding Agent* (ICLR 2025 SSI-FM workshop, arXiv:2504.15228), reports 17→53% on SWE Bench Verified for an agent that autonomously edits its own code; our PR-gated approach is the safety-bounded variant of that pattern — same loop (read state, diagnose, edit code, validate), with the operator review step in place of automatic self-merge. We do not adopt their auto-merge — the human review gate is the explicit safety boundary.

---

## 8. Open-Source First / Prior-Art Reuse

Before any new component, do a prior-art search; prefer reuse over rewriting.

- Search GitHub, PyPI/crates.io, recent papers (with code). `strategy-researcher` (`infrastructure.md §8.5`) + `prior-art-scout` (§2) lead.
- Every PR introducing a non-trivial new component must include a `prior-art` note: what considered, what selected, and — if rewriting — explicit reason.
- Bias: fork + minimal patches > rewrite. Forks declare upstream + sync cadence in `docs/forks.md`.
- Examples to evaluate: Polymarket SDK clients (Python/TS), agent orchestration (`langgraph`, `dspy`, `pydantic-ai`), order management (`ccxt`).
- License: production may depend only on MIT/BSD/Apache-2/MPL-2. Copyleft (GPL/AGPL) requires legal review + PR sign-off.

**Concrete prior-art adopted.** From a survey of *Prediction Arena* (Arcada Labs, `predictionarena.ai`): the four-stage cycle spine Receive → Review → Analyze → Decide (`trading.md §5`); per-cycle prompt assembly with recent settlements/recent trades/previous-cycle reasoning/critical-learning section (`trading.md §2`); the **dual knowledge management** pattern from PA's Polymarket implementation — per-agent `manage_notes` scratchpad (~50 × ~200 words, LRU) for ad-hoc memory + structured `manage_beliefs` store typed by domain for first-class market views with revision history (`trading.md §2`, `infrastructure.md §3`); bid-based mark-to-market valuation (`trading_feedback.md §5`); the three-gate risk model — 15% per-market concentration + solvency + per-cycle spending cap (`infrastructure.md §2`); model-determined sizing within the gates (`trading.md §6`); marketable-limit immediate execution (`infrastructure.md §4`); the lean two-mode operation (paper / real-capital) replacing the deeper backtest harness (`infrastructure.md §8.9`).

From other prior art:
- **TradingAgents** (Tauric Research, arXiv:2412.20138, v0.2.0 Feb 2026) — the 7-role financial-LLM ensemble structure (Fundamentals/Sentiment/News/Technical Analyst, Researcher, Trader, Risk Manager) is direct prior art for our 7-persona ensemble in `trading.md §2`. We adapt the role taxonomy to prediction-market specifics; the structural pattern is confirmed.
- **Voyager** (Wang et al., NeurIPS 2024) — the named-executable-skill-library pattern is the basis for the Strategy Skill Library in `trading.md §8`. Skills are versioned, named, callable by trading agents through a dedicated `call_skill` tool.
- **Reflexion** (Shinn et al., NeurIPS 2023) and **Multi-Agent Reflexion** (MAR, 2025) — natural-language-reflections-as-episodic-memory is the basis for the `lessons` → `patterns` flow (`trading_feedback.md §4`, this file §2). Cross-agent critique is `meta-reviewer`'s role.
- **Self-Improving Coding Agent** (Robeyns et al., arXiv:2504.15228, ICLR 2025 SSI-FM) — external validation that an agent editing its own code through PRs can yield substantial performance gains (17→53% on SWE Bench Verified). We adopt the loop, replace auto-merge with operator-merge.

Rejected or independently re-derived: single-OpenAI-web-search tooling (we keep cloud-only Anthropic per `trading.md §2` with Tavily/Brave for search), pooled real-capital structure, single-model-per-cycle competition (we run a 7-persona ensemble of one model). The fresh-team-per-cycle process model (`infrastructure.md §1`), `cycle_plan` + `operating_doctrine` memory layers (`trading.md §2`), and cloud-only rule are independently designed.

---

## 9. Future Extensions

**RL (v2 — contingent on Anthropic managed fine-tuning for Opus):** if/when exposed, train per-agent variants on resolved-prediction data with PnL-weighted reward. Maintain frozen reference copies for diversity. Shadow-A/B before promotion. Until then, learning loop = prompt optimization (below).

**Meta-learning / prompt optimization:** automated prompt search (DSPy/TextGrad) using historical PnL/hit-rate as objective. Spawn mutated-prompt candidates in shadow paper; promote on 30d outperformance.

**Multi-model cloud ensemble (v2):** add Claude-family models (Sonnet, Haiku) as supplementary backbones — only if performance shows uncorrelated errors with Opus. Cross-vendor cloud only if same test passes *and* audit/data-residency review approves; Claude family preferred. **`trading.md §2` cloud-only rule is non-negotiable** — no local/on-prem in any v2+. Each new model goes through 30d shadow (`trading_feedback.md §7`).

**Cross-venue arbitrage:** Kalshi + sportsbook integration; statistical arb engine across venues. (Adapter pattern already in place — `infrastructure.md §4`.)

**Real-time event-driven:** sub-second news → trade pipeline (lightweight classifier + dedicated agent); direct microstructure stream for thinner markets.

**Privacy/security maturation:** HSM-backed signing (production hardware); SOC2-style controls if scaling to external capital; ZK proofs for prediction provenance (research).

**Scalar/categorical markets:** beyond binary; new aggregation logic.

**Skill-library expansion** (`trading.md §8`): as patterns mature, more procedural knowledge gets crystallized into named skills. Track skill invocation rates; retire skills no agent calls.

**Formal MAB for explore/exploit ratio** (§3.1): if reward attribution becomes faster and operator wants to delegate the ratio decision, replace the fixed rule with f-dsw Thompson Sampling.

---

## 10. Failure Modes — Tier 2

| Failure | Mitigation |
|---|---|
| Code-Evaluation agent overfits to recent noise | Require ≥ 90d data + significance threshold per proposal |
| `patterns` table bloats with low-value entries | Quarterly `meta-reviewer` prune; confidence-decay on stale patterns |
| Adversarial drift (proposes prompts that game metric) | All metrics validated on held-out forward window before promotion |
| Proposal queue grows into noise | `meta-reviewer` suppresses low-priority; operator sees top-K only |
| Code-Evaluation agents converge on bad direction | Mandatory human-in-the-loop; 2-human rule on risk/strategy/explore |
| `lessons` table self-contradicts | `status` field tracks supersession; `meta-reviewer` reconciles in monthly retro |
| Anthropic outage stalls weekly batch | Batch is non-realtime; defer to next cycle, no live impact |
| Code-Evaluation agent suggests bypassing `risk/` | `infrastructure.md §8.4` hook + import-linter prevent the diff existing |
| Whipsaw violations (rapid strategy churn) | §4 enforces minimum strategy lifetime; `meta-reviewer` defers offending proposals; operator override available |
| Explore proposals with no kill-criterion slip through | PR template enforces kill-criterion field; `meta-reviewer` auto-rejects PRs missing it |
| Capital-allocation feedback (`infrastructure.md §7`) rewards luck not skill | Adaptation-quality bonus capped at +10%; primary signal is rolling 30d hit rate + PnL with adequate `n_samples` |

---

## See also

- `trading.md` — the trading runtime that the Tuning Loop targets.
- `trading_feedback.md` — Tier 1 evaluation that produces the `lessons` this team consumes.
- `infrastructure.md` — schemas, central settings, hooks, branch protection, all infrastructure mechanics.
- `index.md` — diagram, glossary, navigation.
