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
