# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(Security tab, "Report a vulnerability"). Do not open a public issue for
anything that could affect signing keys, order placement, or capital limits.
You will get an acknowledgement within a few days. This is a single-operator
project, so there is no security team and no bug bounty.

## What is in scope

- The risk gates under `src/risk/` and the hard capital cap in
  `src/risk/capital_gate.py`.
- Key handling in `src/shared/adapters/key_provider*.py` (encrypted wallet
  file, passphrase from the environment, no key material on disk in clear).
- Order placement and idempotency in `src/shared/adapters/polymarket.py`.
- The Claude Code hooks under `.claude/hooks/` that guard `src/risk/**`,
  `.env` writes, force-pushes, and real-money prompts.

## How secrets are kept out of this repository

- `.env`, `*.key`, `*.pem`, `secrets/`, the encrypted wallet file, and all
  local Claude Code settings are gitignored. The wallet file lives outside the
  repository by default (`~/.config/polymarket-trading/wallet.json`).
- CI runs `gitleaks` and `trufflehog` on every pull request and push to
  `main`. Both are required status checks for merging.
- A pre-commit hook runs `gitleaks` locally, and a Claude Code stop hook runs
  it again before an agent turn ends.
- The public mirror of this repository was built from a history-rewritten
  clone with personal identifiers removed. See the last entry in
  `AUDIT_LOG.md`.

## Default posture

A clean checkout runs in paper mode and cannot trade real capital. Switching
to `real_capital` requires a settings change, a pull request, and an
`AUDIT_LOG.md` entry, and even then `MAX_CAPITAL_EUR` is hardcoded to zero.
