# ADR 0001 — Seven-phase build order

- **Status:** Accepted
- **Date:** 2026-05-01
- **Deciders:** facchini-mika (solo operator)

## Context

This project builds an autonomous trading system on Polymarket whose
`real_capital` execution path can lose money. A naive code-first approach
risks adding safety mechanisms (CI gates, branch protection, audit logging,
risk-layer coverage requirements, hook-enforced plan mode for `src/risk/**`)
only after the code they should protect already exists. We want each
protective layer in place **before** the code it protects.

A second concern is the bootstrap circularity between layers: e.g. Claude
Code hooks that validate Pydantic artifacts cannot be tested before the
Pydantic models exist, but the models should not be merged before the
hooks that protect them are at least in place.

## Decision

Build in seven phases, in this order:

0. **Coding-standards layer** — `pyproject.toml`, `CLAUDE.md`,
   `.pre-commit-config.yaml`, `.gitignore`. (Done: commit `60aece8`.)
1. **Git/GitHub layer** — CI workflows, `CODEOWNERS`, `PULL_REQUEST_TEMPLATE.md`,
   `AUDIT_LOG.md`, ADR directory; branch protection on `main`.
2. **Claude Code config layer** — `.claude/settings.json`, 9 hooks (some as
   stubs gated by `try/except ImportError` until Phase 3), agent skeletons,
   team spec.
3. **Foundation code** — settings, models, risk gates, DB schema + roles,
   adapter protocols. Sequential and blocking. Hook stubs from Phase 2
   are sharpened at the end of this phase.
4. **Four parallel streams** — adapter implementations, `outcome_ingestion`,
   `lessons_summary`, trading-team wiring.
5. **Integration** — cron wiring, structured logging, end-to-end paper-cycle
   test.
6. **First live paper cycle** — real Polymarket read-API + `PaperTradingAdapter`,
   no real-capital exposure (`MAX_CAPITAL_EUR=0` blocks it by design).

CI gates are activated **in step** with the phases that produce the code they
gate, so the repository is always buildable:

- Phase 1: `ruff`, `mypy --strict`, `gitleaks`, `trufflehog`.
- Phase 3: `+ pytest`, `+ import-linter`, `+ src/risk/` 100% coverage,
  `+ alembic up/down` smoketest.
- Phase 4: `+ Hypothesis` property tests, stricter `import-linter` layers.
- Phase 5: `+` end-to-end paper-cycle job (Postgres service container,
  fake Gamma + fake CLOB).

## Consequences

- The first three phases produce no productive code. They produce the
  conditions under which Phase 3+ can be merged with confidence.
- The Phase-2/Phase-3 circularity (hooks need Pydantic models that do not
  yet exist) is resolved with `try/except ImportError: sys.exit(0)`
  wrappers in the relevant hooks. The wrappers are removed as the last
  step of Phase 3.
- This is a **single-operator project**. GitHub forbids self-approval, so
  any `required_approving_review_count ≥ 1` makes every PR un-mergeable
  except by admin bypass. A doctrine that relies on bypass is dishonest,
  so branch protection runs with `required_approving_review_count: 0`.
  Risk-sensitivity for `src/risk/**`, `MAX_CAPITAL_EUR`, and `TRADING_MODE`
  flips is enforced by the audit-log self-review pattern: each such PR
  must add an `AUDIT_LOG.md` entry that documents what changed, what
  could go wrong, and why it's still safe. CI cannot enforce the audit
  entry; operator discipline does. (See ADR 0002 if and when a second
  human ever joins — at that point `required_approving_review_count`
  goes back to ≥1.)
- `MAX_CAPITAL_EUR` defaults to `0` and only an explicit ≥2-reviewer PR
  can raise it. A clean checkout cannot trade real capital.

## Triggers for re-evaluation

- Phase 4 stalls because the Phase-3 frozen interfaces are insufficient
  (suggests interfaces should have been negotiated less rigidly).
- The solo-operator audit-discipline pattern proves too easy to skip in
  practice (suggests a CI check that asserts an `AUDIT_LOG.md` diff in any
  PR touching `src/risk/**` or `MAX_CAPITAL_EUR`).

## References

- `plan.md` — authoritative source for the seven-phase build order.
- `specs/engineering.md §3, §4, §5, §8, §9, §10` — reviewer rule, mode-flip,
  repo layout, audit log, hooks, settings discipline.
- `CLAUDE.md` — daily-driver coding guard rails.
