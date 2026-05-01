# Polymarket Autonomous Trading System — Specifications

**This spec has been split into four component files matching the system architecture.** This file is kept as a thin redirect so existing references in `CLAUDE.md`, hooks, project memory, and prior commit messages continue to resolve.

## Where to read

Start with **[index.md](./index.md)** — architecture diagram, glossary, navigation, and the old-§-to-new-file mapping table for chasing legacy references.

The four component files:

| File | Owns |
|---|---|
| [trading.md](./trading.md) | The executing trading instance — per-cycle Trading Team, 7-persona agent ensemble, strategy layer, decision logic, per-trade gates as the trading layer sees them, the Strategy Skill Library |
| [infrastructure.md](./infrastructure.md) | Platform layer — prediction-market interface (Polymarket primary, Kalshi via shared adapter), data layer + all schemas, execution mechanics, observability, safety controls, central settings, hooks, secret management |
| [trading_feedback.md](./trading_feedback.md) | Tier 1 evaluation — Trade Evaluation Team (1-min cron), result computation, learning generation, evaluation metrics, paper-mode promotion guidance, Strategy Update Loop |
| [optimization.md](./optimization.md) | Tier 2 evaluation — Code Evaluation Team (daily/weekly batches), exploit + explore tracks, anti-whipsaw rule, Tuning Loop, review counts, prior-art reuse, future extensions |

## Why this is split

The four-block architecture (Trading / Infrastructure / Trading Feedback / Optimization) maps one-to-one to these files. The two feedback loops (Strategy Update Loop, Tuning Loop) are explicit subsections in the corresponding files. The split keeps each block reviewable in isolation and makes single-concept changes touch a single file.

See `index.md §4` for the full mapping from old `specs.md §X.Y` references to their new locations.
