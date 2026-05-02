# Audit Log

Append-only. Never edit or delete entries. This is a **single-operator
project**: GitHub forbids self-approval, so branch protection runs with
0 required approvals. The audit log is the only second-review trail.

Every PR that touches one of the following must have an entry here:

- `TRADING_MODE` flip (paper ↔ real_capital)
- `MAX_CAPITAL_EUR` change (any direction)
- Any change under `risk/**`
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
  `settings.json` registering all 9 hooks from `engineering.md §9`
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
  doctrine cannot rely on bypass. Rewrote `CLAUDE.md`, `engineering.md`
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
