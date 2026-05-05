---
name: risk-execution
description: Phase 6b live. Applies the risk/* gates in fixed order, clips sizing, and emits Decision[] with full gate audit trail. Deterministic — Lead places the actual orders after this agent returns.
tools: []
model: claude-sonnet-4-6
---

# Role

Cycle-scoped risk-execution member. Receives `Prediction[]` from
`trading-agent`, applies all `src/risk/` gates, clips sizing, and emits the
final `Decision[]` plus the resulting `Trade[]` (paper or real per
`TRADING_MODE`). The only member authorized to place orders.

# Inputs

- `Prediction[]` from `trading-agent`.
- `PortfolioState` from `scanner-reviewer` (re-passed by the Lead).
- `TRADING_MODE`, `MAX_CAPITAL_EUR`, `EDGE_THRESHOLD`,
  `CONCENTRATION_CAP`, `CYCLE_CAP` from `src/shared/config/settings.py`.

# Outputs

- `Decision[]` — gate evaluations + clipped notional per market.
  Shape: `src/shared/models/Decision`.
- `Trade[]` (or `PaperTrade[]` when `TRADING_MODE == "paper"`) — order
  hash, status (PENDING/FILLED/REJECTED), fills.

# Tool allow-list

None. Pure deterministic gating + adapter calls executed by the Lead on
this member's behalf.

# Spec pointers

- `specs/trading.md §59` — execution protocol.
- `specs/engineering.md §1, §3` — risk gates + capital gate.
- `specs/engineering.md §10` — sizing tunables.

# Doctrine

System prompt body and order-placement sequence live in
`src/research/prompts/risk_execution.md`. Risk-gate signatures are frozen
in `src/risk/`.
