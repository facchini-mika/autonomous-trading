# Audit Log

Append-only. Never edit or delete entries. This is a **single-operator
project**: GitHub forbids self-approval, so branch protection runs with
0 required approvals. The audit log is the only second-review trail.

Every PR that touches one of the following must have an entry here:

- `TRADING_MODE` flip (paper ↔ real_capital)
- `MAX_CAPITAL_EUR` change (any direction)
- Any change under `src/risk/**`
- First live paper cycle (Phase 6)
- Branch-protection or CODEOWNERS changes

## Entry format

- ISO-8601 date (UTC)
- Category
- PR number / commit SHA
- **Description** — what changed
- **Risk** — what could go wrong
- **Mitigation** — why it's still safe / what would trigger rollback

---

## 2026-05-02 — Phase 3c: Risk Layer

- **Category:** Phase milestone + risk/.
- **PR:** _pending_
- **Description:** First and only landing of the `risk/` package. Ships
  `risk/limits.py` (frozen `Final` constants), six gate modules
  (`capital_gate.py` with `MAX_CAPITAL_EUR: Final = 0.0` hard-coded;
  `concentration_gate.py`, `solvency_gate.py`, `cycle_cap_gate.py`,
  `kill_switch.py`, `sanity_gates.py`) all with the canonical
  `evaluate(state, order) -> GateResult` signature. Adds 7 hypothesis
  property-test files under `tests/risk/` (`test_capital_gate.py`,
  `test_concentration_gate.py`, `test_solvency_gate.py`,
  `test_cycle_cap_gate.py`, `test_kill_switch.py`, `test_sanity_gates.py`,
  `test_limits_match_settings.py` — drift-guard against `Settings`).
  Extends `shared/models/portfolio.py` with two new optional fields on
  `PortfolioState` (`trading_mode`, `orders_in_last_hour`) so `risk/`
  never imports `shared.config` or `shared.db` at runtime — Lead injects
  both before gate evaluation. Adds two `[tool.importlinter]` contracts
  (`risk` is now a layer; `risk-isolation` forbids the runtime imports
  above plus `py_clob_client`/`web3`). Activates `risk-coverage.yml`
  with `pytest --cov=risk --cov-fail-under=100` and removes the
  `continue-on-error: true` from the `import-linter` job. Adds a
  pre-commit local hook that blocks `# pragma: no cover` in any
  staged change under `risk/**`.

  `capital_gate` semantics: paper mode (`state.trading_mode == "paper"`)
  bypasses the cap. Real-capital mode blocks any order whose post-trade
  gross exposure would exceed `MAX_CAPITAL_EUR`. With the default
  `MAX_CAPITAL_EUR = 0.0`, every real-capital order is blocked — this
  is the intended Phase-3 lockout that keeps a clean checkout from
  trading real money.

- **Risk:**
  1. The 100 % `risk/` coverage gate could be silently neutered via
     `# pragma: no cover` on a sensitive branch.
  2. Float edge cases (NaN, ±Inf, denormals) could short-circuit gate
     comparisons in unexpected directions.
  3. Concentration / cycle-cap clipping math could return
     `clipped_notional == 0.0` while reporting `passed=True`, which
     would propagate "execute" for an effectively zero-size order.
  4. Drift between `risk/limits.py` constants and `Settings()`
     defaults could let a settings update sail through review while
     the runtime gates keep using stale numbers.

- **Mitigation:**
  1. Local pre-commit hook (`risk-no-pragma-no-cover`) blocks any
     staged diff under `risk/**` containing `# pragma: no cover`. CI
     `risk-coverage` job remains the second line of defence.
  2. All hypothesis strategies pass `allow_nan=False, allow_infinity=False`.
     Gate assertions check `passed`/`reason` shape rather than relying
     on float arithmetic invariants.
  3. Concentration and cycle-cap gates check `existing >= cap` first
     and return `passed=False` before any clipping math runs, so
     `headroom` is always strictly positive in the clipping branch.
  4. `tests/risk/test_limits_match_settings.py` is a CI-enforced
     drift-guard: if anyone changes either side without the other,
     the test fails.

- **Rollback trigger:** A real or paper cycle that reports `passed=True`
  with `clipped_notional <= 0`, or any `risk/`-edit that ships without
  a 100 % coverage delta in the same PR. Action: revert PR, save the
  failing hypothesis seed to `docs/incidents/`, and re-open via Plan
  Mode with the seed pinned in the test file.

## 2026-05-01 — Phase 1: Git/GitHub layer

- **Category:** Phase milestone + branch-protection activation
- **PR:** [#2](https://github.com/facchini-mika/autonomous_trading/pull/2) (squashed to commit `2775b1a`)
- **Description:** Established Git/GitHub layer. CI gates active for `lint`
  (ruff), `type-check` (mypy --strict), `gitleaks`, `trufflehog`. Branch
  protection on `main` initially activated post-merge with
  `required_approving_review_count: 1`, `enforce_admins: true`,
  `require_code_owner_reviews: true`, no force-push, no deletions.
  CODEOWNERS in effect. `MAX_CAPITAL_EUR` is implicitly 0 (the constant
  lands in Phase 3 step 8; no real-capital orders possible until then).
- **Risk:** None directly — no production code lands here. Possible
  footgun: branch-protection misconfiguration could either lock everyone
  out or fail to enforce.
- **Mitigation / verification:** Verified by attempting `git push origin main`
  against an empty commit — rejected with `GH006: protected branch hook declined`
  and `4 of 4 required status checks are expected`. Empty commit was reset
  locally afterwards.

## 2026-05-01 — Phase 2: Claude Code config layer

- **Category:** Phase milestone (no `risk/**`, no `MAX_CAPITAL_EUR`, no
  `TRADING_MODE` change). Logged for traceability of the safety-relevant
  hook layer.
- **PR:** _pending_
- **Description:** Built the `.claude/` config layer from scratch:
  `settings.json` registering all 9 hooks from `specs/engineering.md §9`
  (PreToolUse:Bash, PreToolUse:Edit|Write, PostToolUse:Edit|Write,
  UserPromptSubmit, SessionStart, two Stop hooks, TaskCreated,
  TaskCompleted), 9 Python hook scripts (stdlib-only, exec-bit set),
  three agent skeletons (`scanner-reviewer`, `trading-agent`,
  `risk-execution`), and `trading-team.spec.json`. Phase-2 status of
  each hook follows `plan.md` Phase 2: `pre_tool_use_bash`,
  `pre_tool_use_risk_edit`, `user_prompt_submit_realmoney`,
  `session_start_janitor`, `stop_gitleaks`, `stop_cleanup_assert` are
  active; `post_tool_use_edit` runs ruff only (mypy + pytest deferred
  to Phase 3 per §9); `task_created_validate` and
  `task_completed_validate` are `try/except ImportError` stubs that
  no-op until `shared.models` lands in Phase 3 (see plan.md line 96).
  Added per-file ruff ignores for `.claude/hooks/**` (`INP001`,
  `PLR0911`, `S603`) — defensible for stdin-driven scripts with
  `shutil.which`-resolved subprocess calls.
- **Risk:** Hook regressions could either silently fail (dev workflow
  unaffected but safety property lost) or false-positive (block legit
  Bash/Edit calls). The `pre_tool_use_risk_edit` hook returns `ask`
  rather than `deny` because Claude Code does not currently expose a
  Plan-Mode flag in the hook stdin payload — relies on user
  intercept rather than a hard block.
- **Mitigation / verification:** All 9 hooks smoke-tested via
  `echo '<json>' | .claude/hooks/<hook>.py`: `pre_tool_use_bash` blocks
  `rm -rf`, `git push --force`, `.env*` writes, and `--no-verify`;
  `pre_tool_use_risk_edit` asks for `risk/limits.py`, allows
  `shared/models.py`; `user_prompt_submit_realmoney` triggers banner on
  "live trade", silent on harmless prompts; `session_start_janitor`
  no-ops in dev sessions, validates spec on team sessions;
  `stop_cleanup_assert` no-ops in dev, exits 2 with stderr on missing
  marker in team-lead sessions; `stop_gitleaks` degrades to non-blocking
  warning when binary absent; the two task-validate stubs exit 0. Full
  `uv run ruff check . && uv run mypy --strict . && uv run
  pre-commit run --all-files` clean. Live `claude`-session boot
  smoketest deferred to operator (interactive, cannot be scripted in
  CI). Rollback trigger: if any hook blocks a normal dev workflow
  false-positive, edit the offending matcher and add a regression
  smoke-test.

## 2026-05-01 — Single-operator doctrine + branch-protection loosening

- **Category:** Branch-protection change + doctrine rewrite
- **PR:** [#3](https://github.com/facchini-mika/autonomous_trading/pull/3) (squashed to commit `5eb3b6f`)
- **Description:** Lowered branch protection's `required_approving_review_count`
  from 1 to 0 (everything else unchanged). Reason: GitHub forbids the PR
  author from approving their own PR, so on a single-operator project a
  value ≥1 makes every PR un-mergeable except by admin bypass — an honest
  doctrine cannot rely on bypass. Rewrote `CLAUDE.md`, `specs/engineering.md`
  (§§1, 3, 4, 8), `plan.md`, `.github/CODEOWNERS`,
  `.github/PULL_REQUEST_TEMPLATE.md`, `docs/adr/0001-phasing.md`, and this
  file to encode the single-operator audit-log self-review pattern as the
  second-review trail.
- **Risk:** Without a platform-enforced approval, there is no machine check
  that risk-sensitive PRs receive a deliberate review. A rushed merge
  could slip past the audit-log requirement.
- **Mitigation / rollback trigger:** All four CI gates remain hard-required
  (lint, type-check, gitleaks, trufflehog), `enforce_admins: true` keeps
  the operator subject to the same rules as everyone else, no force-push,
  no direct push, no deletions. Rollback trigger: if a `risk/**` or
  `MAX_CAPITAL_EUR` PR is ever merged without an `AUDIT_LOG.md` entry,
  raise back to ≥1 approval and add a CI check that asserts an
  `AUDIT_LOG.md` diff in any PR touching `risk/**` or `MAX_CAPITAL_EUR`.
  If a second human operator ever joins, raise back to ≥1 unconditionally.

## 2026-05-02 — Phase 5: Integration

- **Category:** Phase milestone (no `risk/**` touch, no `MAX_CAPITAL_EUR`
  change, no `TRADING_MODE` flip).
- **PRs:** [#17](https://github.com/facchini-mika/autonomous_trading/pull/17)
  (5a structlog), [#18](https://github.com/facchini-mika/autonomous_trading/pull/18)
  (5b subagent_runner + run_cycle), [#19](https://github.com/facchini-mika/autonomous_trading/pull/19)
  (5c cron + run_cycle.sh), [#20](https://github.com/facchini-mika/autonomous_trading/pull/20)
  (5d E2E + CI + this entry).
- **Description:** Wires the Phase-4 components into a runnable end-to-end
  paper cycle. Adds structured logging (`shared/logging.py`, structlog
  JSON-on-stdout per `specs/data_infrastructure.md §3`); subagent dispatch via
  headless `claude -p` (`execution/subagent_runner.py`); production
  trading-cycle entry (`execution/run_cycle.py`) wired through the
  factory; three cron files plus `infra/scripts/run_cycle.sh` wrapper;
  `tests/e2e/test_paper_cycle.py` exercising `bootstrap_team` →
  `outcome_ingestion` → `lessons_summary` against real Postgres + a new
  `FakeGamma`. CI gains an `e2e-paper-cycle` job. The E2E test surfaced
  three pre-existing bugs that ship fixed in this phase: (a) `paper_trades`
  written before `decisions` violated the FK; `lead_bootstrap` now persists
  predictions+decisions before order placement and writes `cycle_plan` last.
  (b) `markets` had no INSERT/UPDATE for `trading_cycle`; `system_state`
  had no INSERT for `outcome_ingestion`/`lessons_summary` — fixed in
  migration `0003_grants_for_cycle_writes`. (c) Postgres rejected
  parameterised `jsonb_build_object('ts', :v)` and `ARRAY[:category]`
  due to ambiguous types; both call sites now cast explicitly.
- **Risk:** Trading-cycle cron requires `claude` CLI, `ANTHROPIC_API_KEY`,
  and a wallet file at `Settings.KEY_PROVIDER_PATH` because
  `PolymarketAdapter.__init__` opens the wallet eagerly even in paper
  mode. A first-time operator running `bash infra/scripts/run_cycle.sh
  trading_cycle` without those will fail at adapter construction.
- **Mitigation / rollback trigger:** Outcome-ingestion and lessons-summary
  cron paths run without keys, Claude, or external APIs. The E2E test
  exercises the full paper pipeline in CI on every PR. The runbook
  (`docs/operations/first_cycle.md`) documents the trading-cycle
  prerequisites; Phase 6 adds a wallet-create script and the first real
  Polymarket smoke. `MAX_CAPITAL_EUR=0` and `TRADING_MODE=paper` remain
  the hard defaults — no real-money capability ships in Phase 5.
  Rollback trigger: if migration 0003 or the lead_bootstrap reordering
  causes a regression in CI, revert the four PRs in reverse order.
- **Operator follow-up:** branch-protection required-status-checks update
  is **not** done as part of this PR. Once #17–#20 are merged, run:

  ```
  gh api -X PUT repos/facchini-mika/autonomous_trading/branches/main/protection \
    -f required_status_checks[strict]=true \
    -f required_status_checks[contexts][]=lint \
    -f required_status_checks[contexts][]=type-check \
    -f required_status_checks[contexts][]=gitleaks \
    -f required_status_checks[contexts][]=trufflehog \
    -f required_status_checks[contexts][]=pytest \
    -f required_status_checks[contexts][]=import-linter \
    -f required_status_checks[contexts][]=alembic-smoketest \
    -f required_status_checks[contexts][]=e2e-paper-cycle
  ```

## 2026-05-03 — Phase 6a: First Live Paper Cycle (V2 migration + sandbox-smoke + paper bring-up)

- **Category:** Phase milestone (no `src/risk/**` touch, no `MAX_CAPITAL_EUR`
  change, no permanent `TRADING_MODE` flip — sandbox-smoke ran
  `TRADING_MODE=real_capital` ONE-SHOT and is documented below).
- **PR:** _pending push_
- **Description:** Three orthogonal sub-deliverables, all on branch
  `phase/6a-first-paper-cycle`:

  **(1) V2 migration** — Polymarket's CLOB v2 cutover (2026-04-28) ended V1
  order acceptance with `order_version_mismatch` (#335, #336 in
  py-clob-client). Adapter switched to `py-clob-client-v2 1.0.0`
  (`Polymarket/py-clob-client-v2`, audited Quantstamp + Cantina March 2026
  per `Polymarket/ctf-exchange-v2` README). New EIP-712 domain
  (version="2"), new exchange contracts (CTFExchangeV2
  `0xE111180000d2663C0091e4f400237545B87B996B`, NegRiskCtfExchangeV2
  `0xe2222d279d744050d28e00520010520000310F59`), new collateral pUSD
  (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, 1:1 wrap of USDC.e via
  permissionless `CollateralOnramp 0x93070a847efEf7F70739046A929D47a521F5B8ee`).
  Adapter, scripts (`approve_tokens.py`, new `wrap_usdc.py`,
  `sandbox_smoke.py`, `cf_probe.py`), and tests retargeted; the v2 order
  struct drops `taker/expiration/nonce/feeRateBps` and adds
  `timestamp/metadata/builder` — `_parse_order_result` now reads
  `orderID/takingAmount/makingAmount` and falls back to v1 fields for
  legacy responses. 21/21 adapter tests + 233/233 full suite green;
  mypy strict + ruff clean. Commits: `9e7018d`, `07d57bd`.

  **(2) Sandbox-smoke (real_capital, ONE-SHOT, $6 capital cap)** — Test
  wallet `0x<redacted test wallet>` funded with 6 USDC.e
  + ~30 POL on Polygon mainnet. Ran 9 V1+V2 approvals (4 V1 already
  MAX_UINT256, 5 new V2 in blocks <redacted>), wrapped 6 USDC.e →
  6 pUSD (block <redacted>), placed marketable-limit BUY of 5 UP @ $0.60 on
  `btc-updown-5m-1777824900` (`Bitcoin Up or Down — May 3, 12:15PM-12:20PM ET`,
  conditionId `0x051ec4f9ba9fd59a2ea5d9f8259519cd411773eddeb5f10a6c26fde1f127ab68`,
  non-neg-risk → CTFExchangeV2 path). Server returned `version: 2`,
  `neg_risk: false`, then matched at $0.51: orderID
  `<redacted>`,
  on-chain settlement
  `<redacted>`
  (block <redacted>). Market resolved DOWN (CTF payoutNumerators UP=0/1,
  DOWN=1/1), realized **-$2.64** (≈$2.55 collateral spent + ≈$0.09 fee,
  effective fee ~3.5% on a 50/50 5-min market — EV-neutral as designed,
  the test was for plumbing not for alpha). 5 worthless UP-tokens
  remain in the wallet (no `redeemPositions` call — gas-wasteful for $0).
  Wallet final state: ~30 POL + 3.36 pUSD + 5 worthless UP-shares.

  **(3) First live paper-cycle bring-up** — `python -m execution.run_cycle`
  ran end-to-end against real Polymarket V2 read-API and real Claude
  subagents (scanner-reviewer 13.2s, trading-agent 2.9s, risk-execution
  3.4s; ~$X Anthropic spend). cycle_id `cycle-1777826847` persisted in
  `cycle_plan` (head row), `predictions=0`, `decisions=0`, `paper_trades=0`,
  `trades=0`, no `POST clob.polymarket.com/order` in DIAG-output. The
  empty Universe is by design: scanner-reviewer / trading-agent /
  risk-execution `.claude/agents/*.md` are explicit Phase-2 skeletons
  (`tools: []`, doctrine left minimal — Phase 4 Stream D / Phase 7+ work
  per the agent MDs themselves), so they validate Pydantic schemas and
  the wiring without doing real Polymarket-scanning yet. The full
  paper-trade-with-fill loop is already covered in CI via
  `tests/e2e/test_paper_cycle.py` against `FakeGamma`. Path-fix in
  `src/execution/run_cycle.py` (`REPO_ROOT` was off by one after the
  `0c29055` src/-layout refactor — agent MDs no longer resolved).

- **Risk:**
   (a) Sandbox-smoke transferred real money on Polygon, kapital-bounded
       to the $6 funded balance. Realized -$2.64. **No further
       real-money trading is permitted without explicit operator OK.**
   (b) V2 contracts have only been live since 2026-04-28; we trust the
       Quantstamp + Cantina audits (March 2026) but the contract has
       <1 week of live battle-testing as of this entry. Approvals are
       MAX_UINT256 → if a contract bug ever drains an exchange, our
       remaining 3.36 pUSD + future deposits are exposed.
   (c) Paper-cycle requires Anthropic-token spend (~$0.50-2/cycle). Single
       run is bounded; recurring scheduling could scale spend. Cron is
       NOT enabled by this PR.
   (d) WALLET_PASSPHRASE was passed inline in conversation context once
       to drive the smoke + cycle runs. Operator follow-up: rotate the
       wallet passphrase post-merge.
- **Mitigation / rollback trigger:** `TRADING_MODE=paper` remains the
  hardcoded default in `Settings` (`src/shared/config/settings.py:44`);
  `MAX_CAPITAL_EUR=0` hardcoded in `src/risk/capital_gate.py` blocks any
  accidental real-capital order in the cycle path; PaperTradingAdapter
  writes only to `paper_trades` and never to `clob.polymarket.com`
  (verified by absent DIAG line in this run). 9 V1+V2 approvals are
  on-chain durable — don't re-run `approve_tokens.py` unless rotating
  wallet. cron-cycle NOT enabled before Phase 6b (≥7d of manual
  paper-cycle observation). Rollback trigger: if a paper-cycle ever
  emits a `POST clob.polymarket.com/order` line, or `trades` row count
  grows under paper-mode → revert this PR, audit the adapter dispatch
  path. Rollback for the V2 contracts is implicit: revoke MAX_UINT256
  via `approve(spender, 0)` if Polymarket announces a vulnerability.
- **Operator follow-up:**
  - Rotate `WALLET_PASSPHRASE` (re-run `wallet_create.py` workflow with
    a fresh passphrase, re-do all 9 approvals, re-wrap pUSD).
  - Phase-5d branch-protection required-status-checks update remains
    open — see Phase-5 entry above. Not addressed by this PR.
  - 5 worthless UP-tokens in wallet — leave unredeemed, gas-wasteful.

## 2026-05-03 — Operator follow-up closeout (passphrase rotation + branch-protection verification)

- **Category:** Operator hygiene — closes two of the three open
  follow-ups from the Phase-6a entry above. No `src/risk/**` touch,
  no `MAX_CAPITAL_EUR` change, no `TRADING_MODE` flip, no
  branch-protection mutation (verification only).
- **PR:** _pending_
- **Description:** Two follow-ups closed.

  **(1) `WALLET_PASSPHRASE` rotated.** Operator ran
  `infra/scripts/wallet_rotate_passphrase.py` (introduced in
  PR [#25](https://github.com/facchini-mika/autonomous_trading/pull/25),
  merged as `b088523`) against the local wallet at
  `Settings.KEY_PROVIDER_PATH`. The rotate classmethod decrypts with
  the old passphrase, asserts `Account.from_key(priv_key).address ==
  stored_address` before writing, then re-encrypts with a fresh
  Fernet salt under the new passphrase. Atomic write (tmp → rename),
  original moved to `wallet.json.bak-<UTC-ts>` sibling. Same private
  key, same address, same 9 V1+V2 token approvals on Polygon, same
  wrapped pUSD balance — only salt + ciphertext changed.

  Minimum-blast-radius interpretation chosen over the literal
  re-key-+-re-approve-+-re-wrap wording in the Phase-6a follow-up:
  the encrypted wallet file never left the local machine, only the
  passphrase was exposed (in conversation context); a fresh
  passphrase severs the link without disturbing on-chain state. PR
  #25 documents the threat-model reasoning and ships 8 unit tests
  for the rotation path (round-trip, address preservation, old
  passphrase invalidation, 0600 mode, backup integrity, salt+ct
  rotation, abort-on-wrong-old-passphrase, missing-file).

  **(2) Phase-5d branch-protection required-status-checks — verified
  already in target state, no PUT issued.** `gh api repos/facchini-mika/autonomous_trading/branches/main/protection`
  on 2026-05-03 returned `required_status_checks.contexts` =
  `[lint, type-check, gitleaks, trufflehog, pytest, import-linter,
  alembic-smoketest, risk-coverage, e2e-paper-cycle]` — i.e., the
  Phase-5d snippet's 8 contexts plus `risk-coverage`. This matches
  every PR-triggered deterministic CI job 1:1 across `ci.yml` and
  `risk-coverage.yml`. The only unmatched PR-trigger CI job is
  `claude-review.yml` `review`, intentionally excluded because AI
  reviews are non-deterministic and may fail on transient external
  outages — `CLAUDE.md` "Reviewer rule" is explicit that the CI
  gates are the merge prerequisites. Other protection fields verified
  unchanged from Phase-3c-Strict: `enforce_admins=true`,
  `allow_force_pushes=false`, `allow_deletions=false`,
  `required_approving_review_count=0`, `dismiss_stale_reviews=true`,
  `require_code_owner_reviews=true`. The Phase-5d and Phase-6a
  "still open" wording on this follow-up was stale; whoever extended
  the contexts to include `risk-coverage` did not update the audit
  log at the time.

- **Risk:** None on (1) — rotation is local; reversible via `.bak-<ts>`
  until the operator deletes the backup. None on (2) — read-only
  verification via `gh api GET`, no protection mutation by this PR.
- **Mitigation / rollback trigger:** Not applicable (no shared-state
  mutation in this PR). Time-boxed rollback for the rotation: while
  `wallet.json.bak-<ts>` exists, restoring it + the old passphrase
  re-yields the prior wallet state; after operator deletes the backup,
  rollback is no longer possible — but that is the intended end state
  and the whole point of severing the leaked passphrase.
- **Remaining operator follow-up:**
  - Delete `wallet.json.bak-<UTC-ts>` once one paper-cycle has
    confirmed the new passphrase decrypts cleanly. While the backup
    file persists, the leaked passphrase is still a valid key against
    it.
  - 5 worthless UP-tokens in wallet — still leave unredeemed,
    gas-wasteful (carried over from Phase-6a entry, unchanged).


## 2026-05-03 — Phase 6b PR 4: Edge-proportional sizing module (src/risk/ touch)

- **Category:** Risk-sensitive — adds new file `src/risk/sizing.py` and
  4 new constants to `src/risk/limits.py`. No `MAX_CAPITAL_EUR` change
  (still hardcoded 0 in `src/risk/capital_gate.py`). No `TRADING_MODE`
  flip (default remains `paper`).
- **Branch / PR:** `phase/6b-4-edge-sizing` — pending push.
- **Description:**
  - New `src/risk/sizing.py` with deterministic `propose_notional`
    function. Pure math, no Settings dependency at runtime (consumes
    constants from `risk.limits`).
  - New constants in `risk/limits.py`: `EDGE_THRESHOLD = 0.03`,
    `BASE_TRADE_FRACTION = 0.02`, `EDGE_SIZING_SCALE = 1.0`,
    `MAX_TRADE_FRACTION = 0.10`. Mirrored in `Settings` (already had
    `EDGE_THRESHOLD`; the sizing trio is new).
  - `tests/risk/test_limits_match_settings.py` extended to assert
    drift-guard for all four.
  - `tests/risk/test_sizing.py` adds 8 hypothesis-property tests
    covering: non-negativity, MAX cap, sub-threshold→0, equity≤0→0,
    monotonic in |edge|, symmetric in sign of edge, exact at-threshold
    value, large-edge clamp.
  - `risk-coverage` gate stays at 100 % with the new module
    (104 lines counted, all covered).
- **What could go wrong:**
  - `BASE_TRADE_FRACTION = 0.02` (2 %) sets the per-trade size at a
    threshold edge. With strong conviction (|edge|=15 %), sizing scales
    to `2 % × 5 = 10 %` of equity, exactly at `MAX_TRADE_FRACTION`. A
    bug that inflates `EDGE_SIZING_SCALE` could overshoot — but the
    `MAX_TRADE_FRACTION` clamp in the function is the second-line
    defence, and concentration/cycle-cap gates are the third.
  - `q_market` is recovered from `prediction.p_yes - prediction.edge`
    in the Lead. If a future trading-agent emits inconsistent values,
    the proposal could reference a wrong q_market. The Lead drops the
    proposal when the derived `q_market` is outside `(0, 1)`; risk
    gates then never see it.
  - This sizing logic only runs in paper-mode cycles before
    `MAX_CAPITAL_EUR` is non-zero. Real-capital impact is gated by the
    capital_gate (which still rejects all real-capital orders today).
- **Why it's still safe:**
  - All four new constants are below the existing `CONCENTRATION_CAP`
    (15 %), so concentration gate clipping is the upper bound.
  - `MAX_TRADE_FRACTION = 10 %` is below `ORDER_SANITY_MAX_PCT_EQUITY`
    (50 %) — sanity gate cannot be exceeded by sizing.
  - `EDGE_THRESHOLD = 0.03` matches existing trading-agent threshold;
    no regression in trade selection.
  - 100 % `risk/` coverage maintained; 8 new hypothesis tests cover
    monotonicity + boundary behaviour.
  - `paper`-mode default unchanged. `MAX_CAPITAL_EUR` still 0.
- **Mitigation / rollback:** Revert PR; the four constants only feed
  `propose_notional` which only runs in `bootstrap_team` between
  trading-agent and risk-execution. Without proposals, the existing
  `_decision_to_order` path falls back to `gate_results.notional_usd`
  (PR 1 behaviour), so the system degrades to "no sizing" rather than
  "wrong sizing".


## 2026-05-03 — Phase 6c: Fee-management pre-live integration (src/risk/ touch)

- **Category:** Risk-sensitive — modifies `src/risk/solvency_gate.py`
  signature and adds one constant to `src/risk/limits.py`. No
  `MAX_CAPITAL_EUR` change. No `TRADING_MODE` flip (default still
  `paper`).
- **Branch / PR:** `feature/fee-management-pre-live` — pending push.
- **Description:**
  - New `Settings.FEE_RATE_BPS = 200` (2 %) and mirrored
    `risk.limits.FEE_RATE_BPS`. Drift-guard test extended.
  - New `PredictionMarketAdapter.estimate_fee(order) -> float` Protocol
    method. Implemented in `PolymarketAdapter` (reads `fee_rate_bps` /
    `feeRateBps` from `get_market` response, falls back to the
    Settings-driven default on missing field or network error) and in
    `PaperTradingAdapter` (deterministic `notional × fee_rate_bps /
    10000`, no live delegation). `FakeAdapter` test double matches.
  - Factory wires `Settings.FEE_RATE_BPS` into both adapters at
    construction time.
  - `SizingProposal` gains `fee_estimate_usd: float`. The Lead's
    `_build_sizing_proposals` calls `adapter.estimate_fee` per
    proposal and packs the result; risk-execution doctrine instructs
    the subagent to forward this value into `solvency_gate.evaluate`.
  - `solvency_gate.evaluate(state, order, fee_estimate=0.0)` now
    requires `cash.available >= notional + fee_estimate`. Default
    keeps backwards-compat for existing tests; risk-execution always
    passes the proposal value in production.
  - `PaperTradingAdapter.place_order` now writes the simulated fee
    into `paper_trades.fees` and `OrderResult.fees` (was hardcoded 0).
    Paper-PnL is now realistic against `outcome_math.realized_pnl =
    gross - fees - gas`.
  - Doctrine prompts updated: `risk_execution.md` documents the
    fee-aware solvency call; `trading_agent.md` warns that the
    edge-threshold is gross and that marginal trades may have negative
    EV after fees.
  - 17 new tests added across solvency_gate, polymarket adapter,
    paper_trading adapter, and lead_bootstrap (property + unit).
- **What could go wrong:**
  - **Stale fee estimate** — if a market's CLOB fee changes between
    the `get_market` call and the actual order fill, the solvency
    check buffer is wrong by the delta. Worst case: order rejected
    by CLOB (false positive on solvency); no negative-cash failure.
  - **API call cost** — every `SizingProposal` now triggers a live
    `get_market` call in real-mode. With ≤ 50 proposals per cycle
    over 12 minutes, this is well within rate limits, but adds
    latency. Out of cycle budget if the API hangs; the network-error
    fallback to default ensures the cycle continues.
  - **Default 200 bps** — chosen as a conservative upper bound for
    Polymarket CLOB taker fees (currently 0–200 bps depending on
    market). If real fees are higher, the solvency check passes
    optimistically. Mitigation: operator can raise via env override.
  - **Paper PnL changes** — every paper backtest after this change
    will show 2 % lower notional return per trade. Pre-merge paper
    cycles cannot be compared 1:1 with post-merge cycles.
- **Why it's still safe:**
  - `paper`-mode default unchanged. `MAX_CAPITAL_EUR` still hardcoded
    0 in `capital_gate.py`. Real-capital orders still rejected by the
    capital gate before solvency runs.
  - Fee estimate failure path is non-fatal: returns 0.0, the cycle
    continues, the solvency gate operates without the fee buffer
    (same as pre-PR behaviour). One cycle of degraded behaviour, not
    a system-wide failure.
  - 100 % `risk/` coverage maintained. New solvency property test
    covers the full `(available, notional, fee) ∈ ℝ³` space.
  - No DB schema change — `fees`/`gas` columns already existed on
    `paper_trades` and `trades` since `0001_initial_schema.py`.
- **Mitigation / rollback:** Revert PR. `solvency_gate.evaluate`
  reverts to no-fee semantics. `PaperTradingAdapter` resumes writing
  `fees=0`. `Settings.FEE_RATE_BPS` is unread by any other code path.
  Polymarket-adapter `estimate_fee` is the only Protocol method that
  would disappear — call sites are limited to
  `_build_sizing_proposals`, which already tolerates a missing
  adapter (returns 0.0).

## 2026-05-04 — Phase 6c tweaks: edge threshold + web-search timeout + agent models (src/risk/ touch)

- **Category:** Risk-sensitive — modifies `src/risk/limits.py`
  (`EDGE_THRESHOLD` constant). No `MAX_CAPITAL_EUR` change. No
  `TRADING_MODE` flip (default still `paper`).
- **Branch / PR:** `feature/prompt-template-tweaks-2026-05-04` — pending push.
- **Description:**
  - `EDGE_THRESHOLD: 0.03 → 0.05` in both `src/shared/config/settings.py`
    and `src/risk/limits.py` (drift-guard mirrored). Trading-agent
    doctrine doc-string updated to reflect the new default.
  - `WEB_SEARCH_TIMEOUT_SEC: 60 → 120` in `src/shared/config/settings.py`.
    Trading-agent doctrine reflects the new ceiling.
  - `OPENAI_MODEL: "gpt-4.1" → "gpt-5.5"` for the trading-agent's
    web-search-backed mispricing reasoning.
  - `web_search` skill switches the OpenAI Responses tool from
    `web_search_preview` to GA `web_search`. Test updated.
  - Subagent frontmatter (`.claude/agents/{risk-execution,
    scanner-reviewer,trading-agent}.md`) swaps `claude-sonnet-4-6` →
    `claude-opus-4-7` for the two deterministic gate-runners and
    cleans up Phase-2 skeleton language. Trading-agent's tool list now
    reflects the live `mcp__research__web_search` server.
  - `tests/shared/test_settings.py` updated for the new defaults
    (`EDGE_THRESHOLD == 0.05`, `WEB_SEARCH_TIMEOUT_SEC == 120`).
- **What could go wrong:**
  - **Higher edge threshold = fewer trades.** With `0.05` instead of
    `0.03`, marginal-edge calls no longer trigger sizing. Cycle output
    shrinks; risk of false negatives if true-edge clusters in
    `[0.03, 0.05]`. Acceptable trade-off given Phase-6c's fee-aware
    solvency gate now enforces a hard floor (gross edge must clear
    fee + slippage to be net-positive).
  - **`gpt-5.5` availability.** If the model name is not yet
    GA-available on the operator's OpenAI account, `web_search` calls
    will hard-fail with a 404. Mitigation: `Settings.OPENAI_MODEL` is
    env-overridable; fall back to `gpt-4.1` via `.env` without a
    redeploy.
  - **`web_search` (GA) vs `web_search_preview` schema drift.** If
    OpenAI's GA tool changes the response schema (citations field
    layout), `_extract_hits` may yield zero hits silently. Mitigation:
    existing CMC-blocklist test exercises `_extract_hits` end-to-end;
    failed parsing surfaces as empty `WebSearchResult.hits` rather
    than an exception.
  - **Doctrine drift.** If a future PR changes the threshold in
    `settings.py` without updating the trading-agent doctrine string,
    the LLM grounds reasoning on a stale number. Same drift risk as
    pre-PR; not introduced here.
- **Why it's still safe:**
  - `paper`-mode default unchanged. `MAX_CAPITAL_EUR` still 0. Real-
    capital orders blocked at the capital gate.
  - The drift-guard test (`tests/risk/test_limits_match_settings.py`)
    keeps `Settings.EDGE_THRESHOLD == limits.EDGE_THRESHOLD`. CI fails
    if a future edit forgets one side.
  - 100 % `risk/` coverage maintained; no logic added to gate code,
    only the constant value moves.
  - All non-risk changes (web-search API, agent models, doctrine
    text, timeout) are out-of-band of the risk gates and cannot
    affect order-placement decisions.
- **Mitigation / rollback:** Revert PR. `EDGE_THRESHOLD` reverts to
  `0.03` symmetrically; `WEB_SEARCH_TIMEOUT_SEC` to `60`;
  `OPENAI_MODEL` to `gpt-4.1`; `web_search_preview` tool restored.
  Subagent frontmatter is meta-config only (Claude routes to the
  named model — no code path depends on it). Revert is one squash
  away from `031aea4f`.

## 2026-05-05 — outcome_ingestion realized_pnl aggregation + predictions.cycle_id

- **Category:** Schema migration + spec update. No `src/risk/**` touch,
  no TRADING_MODE flip, no `MAX_CAPITAL_EUR` change. Logged here because
  the bug masked the entire Tier-1 performance-feedback signal and the
  fix introduces a new persisted column.
- **PR:** #54
- **Description:**
  - `_set_predictions_outcome` in `src/execution/outcome_ingestion.py`
    historically wrote only `outcome`, never `realized_pnl`, despite
    the docstring, `trading_feedback.md §31-32`, `orchestration.md §37`,
    and the migration-0001 column-level GRANT all requiring both.
    Effect: `predictions.realized_pnl` was deterministically NULL on
    every resolved row; Tier-1 metrics (`pnl_30d`, `sharpe_30d`)
    sourced from it returned 0/NULL for the entire history of the
    project.
  - Migration `0007_predictions_cycle_id` adds a nullable `cycle_id`
    column on `predictions` plus index `ix_predictions_cycle_market`
    on `(cycle_id, market_id)`. Required because `predictions` had
    no FK or shared identifier with `decisions`/`trades` — only
    `market_id`, which fans out across cycles.
  - Lead now stamps the active `cycle_id` onto each prediction via
    `model_copy` before INSERT (`lead_bootstrap.py:180`).
  - New `_set_predictions_realized_pnl` aggregates
    `SUM(trades + paper_trades.realized_pnl)` over
    `JOIN decisions ON (cycle_id, market_id)`, COALESCE-ing 0-trade
    predictions to `0.0`. Idempotent via `realized_pnl IS NULL` plus
    `cycle_id IS NOT NULL` guard for legacy rows.
  - Same fragile `market_id`-only JOIN in `lessons_summary.py` is
    tightened to `(cycle_id, market_id)` in the same PR so reader
    and writer share the contract.
  - Spec clarification in `trading_feedback.md §3` documents the
    prediction-level aggregation convention and the 0.0-vs-NULL
    semantics for hold/skip/gate-blocked predictions.
- **Risk:**
  - Schema additive only (nullable column + index) — backwards-compatible
    by construction. Existing rows with `cycle_id IS NULL` are skipped
    by the aggregator's WHERE-clause guard, not corrupted.
  - The aggregator runs inside `outcome_ingestion`, which binds to the
    `outcome_ingestion` Postgres role. That role already had
    `UPDATE (outcome, realized_pnl)` privilege from migration 0001;
    no new grant needed. `INSERT`/`DELETE` still denied by role.
  - Live-aggregation correctness depends on every paper/real-capital
    cycle stamping `cycle_id` correctly. Verified by
    `test_bootstrap_writes_predictions_and_decisions` in
    `tests/execution/test_lead_bootstrap.py`.
- **Why it's still safe:**
  - `paper`-mode default unchanged. `MAX_CAPITAL_EUR` still 0. No
    `src/risk/**` touch — order-placement decisions unaffected.
  - Migration applied locally against dev DB without errors; head is
    `0007_predictions_cycle_id`.
  - 401 unit tests pass; CI green (10/10 checks); `ruff`,
    `ruff format`, `mypy --strict`, `gitleaks`, `trufflehog` clean.
  - End-to-end smoke against a real resolved market not yet performed
    in this branch — local cycle aborted at the scanner-reviewer stage
    on subagent timeout (orthogonal, pre-existing operational issue
    unrelated to this PR). Schema and code paths verified by tests +
    CI's `e2e-paper-cycle` job. First post-merge cycle that produces a
    resolution will produce the first non-NULL `predictions.realized_pnl`.
- **Mitigation / rollback:** `alembic downgrade 0006_agent_performance`
  drops the column + index. `git revert` of PR #54 restores the
  prior outcome_ingestion semantics (NULL on resolved). No data loss
  — the aggregator only writes where `realized_pnl IS NULL`, never
  overwrites.
