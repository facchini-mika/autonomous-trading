# Audit Log

Append-only. Never edit or delete entries. Every entry is one of:

- `TRADING_MODE` flip (paper ↔ real_capital)
- `MAX_CAPITAL_EUR` change (increase or decrease)
- First live paper cycle (Phase 6)
- Solo-operator self-approval on a `risk/**` or `MAX_CAPITAL_EUR` PR
  (document two distinct qualitative reviews)

## Entry format

- ISO-8601 date (UTC)
- Category
- PR number / commit SHA
- Description (what, why)
- Reviewer(s) — for 2-approval cases, link both review texts

---

## 2026-05-01 — Phase 1: Git/GitHub layer

- **Category:** Phase milestone
- **PR:** _to be filled in after merge_
- **Description:** Established Git/GitHub layer. CI gates active for `ruff`,
  `mypy --strict`, `gitleaks`, `trufflehog`. Branch protection on `main`
  configured by operator post-merge. CODEOWNERS in effect.
  `MAX_CAPITAL_EUR` is implicitly 0 (the constant lands in Phase 3 step 8;
  no real-capital orders possible until then).
- **Reviewer:** Solo-operator (see CLAUDE.md "Reviewer rule").
