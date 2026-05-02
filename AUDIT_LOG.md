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
