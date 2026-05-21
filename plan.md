# Polymarket Autonomous Trading — MVP Build Plan

Architektonischer Source-of-Truth: [`specs/specs.md`](./specs.md) (+ 6 Sub-Files: `specs/trading.md`, `specs/orchestration.md`, `specs/data_infrastructure.md`, `specs/engineering.md`, `specs/trading_feedback.md`, `specs/optimization.md`).

**Reihenfolge ist binding** — Standards → Git → Skills/Hooks → Code. Plan beginnt bei Phase 0.

User-Memory-Constraints: Local-first, real_capital ab Tag 1, Sprache Deutsch.
Klärungen: GitHub-Remote existiert; KeyProvider-Primary = encrypted-at-rest local file; Polymarket-Tests = Mocks-only in CI + manueller Sandbox-Smoke vor Phase 6.

---

## Reihenfolge-Überblick

```
Phase 0  →  Coding Standards Layer        (CLAUDE.md, pyproject.toml, pre-commit)
Phase 1  →  Git/GitHub Layer              (Branch Protection, CI Gates, AUDIT_LOG)
Phase 2  →  Claude Code Config Layer      (settings.json, 9 Hooks, Agent-Skelette, Team-Spec)
Phase 3  →  Foundation Code (sequenziell) (Settings, Models, Risk, Schema, Adapter-Protocol)
Phase 4  →  4 PARALLELE Streams           (Adapters / outcome_ingestion / lessons_summary / Trading-Cycle-Wiring)
Phase 5  →  Integration                   (Cron-Wiring, Logging, E2E paper-cycle)
Phase 6  →  First Live Paper Cycle        (real Polymarket-Read + PaperAdapter)
```

---

## Phase 0 — Coding-Standards-Layer

**Ziel:** Konventionen einfrieren, bevor eine Code-Zeile geschrieben wird.

**Files:**
- `CLAUDE.md` (specs/engineering.md §6, < 200 Zeilen):
  - Build/Test/Lint-Commands (`uv run pytest`, `uv run ruff`, `uv run mypy --strict`, `uv run alembic upgrade head`).
  - No-Go-Liste (keine Real-Keys, keine Live-Trades ohne Confirm, keine `src/risk/`-Edits außer Plan-Mode, kein direkter `main`-Push).
  - Pointer auf `specs/specs.md` + 6 Sub-Files.
  - Hinweis auf 2-Reviewer-Pflicht für `MAX_CAPITAL_EUR` und `src/risk/`.
  - Default-Mode `paper`.
- `CLAUDE.local.md` (gitignored, persönliche Notizen).
- `pyproject.toml`: uv, Python 3.12, Dependencies (pydantic, pydantic-settings, structlog, psycopg, alembic, py-clob-client, web3, openai, hypothesis, pytest, pytest-cov, ruff, mypy, import-linter).
- `.gitignore`: `.env*`, `CLAUDE.local.md`, `data/`, `__pycache__`, `.venv`.
- `.python-version`: `3.12`.
- `.pre-commit-config.yaml`: gitleaks, trufflehog, ruff, mypy.
- `.editorconfig`, `uv.lock`.

**Exit:** `uv sync` läuft, pre-commit installiert sich, ruff/mypy greifen sauber auf leeres Repo.

---

## Phase 1 — Git/GitHub-Layer

**Ziel:** Branch Protection + CI vor jedem Code.

**Files:**
- `.github/workflows/ci.yml` — Phase-1-minimal: `ruff`, `mypy --strict`, `gitleaks`, `trufflehog`. Läuft grün auf leerem Repo.
- `.github/workflows/risk-coverage.yml` — Skeleton, in Phase 1 noch `if: false`.
- `.github/PULL_REQUEST_TEMPLATE.md` — Sections für Mode-Flip, `MAX_CAPITAL_EUR`-Touch, Reviewer-Count-Erinnerung, Kill-Criterion bei Strategy-Changes.
- `.github/CODEOWNERS` — `src/risk/ @facchini-mika` (kombiniert mit Branch-Protection-"≥2 reviewers" erzwingt 2-Reviewer-Regel auf `src/risk/`).
- `AUDIT_LOG.md` — append-only-Markdown, dokumentiert Mode-Flips + `MAX_CAPITAL_EUR`-Änderungen + erste Live-Paper-Cycles.

**Manuelle Operator-Steps (außerhalb Repo):**
- `gh` CLI vorhanden + authentifiziert.
- Branch-Protection auf `main` über `gh api`: `required_approving_review_count: 0` (Single-Operator-Projekt; GitHub erlaubt kein Self-Approval, daher ist 0 die einzig konsistente Wahl — Risk-Sensitivity läuft über die Audit-Log-Disziplin, siehe `specs/engineering.md §3, §4, §8`); required status checks `lint`/`type-check`/`gitleaks`/`trufflehog` ab Phase 1, `pytest`/`import-linter`/`src/risk/`-Coverage ab Phase 3; `enforce_admins: true`; keine direkten Pushes; kein Force-Push; keine Branch-Deletes.

**CI-Stufung über die Phasen** (verhindert dass Phase-1-Repo nicht baubar ist):
- Phase 1: ruff + mypy + gitleaks + trufflehog (grün auf leerem Repo).
- Phase 3: + `pytest`, + `import-linter`, + `src/risk/`-100%-Coverage (`pytest --cov=risk --cov-fail-under=100`), + Alembic Up/Down-Smoketest.
- Phase 4: + Hypothesis-Property-Tests, + härtere `import-linter`-Layer-Regeln.
- Phase 5: + E2E-Paper-Cycle-Job (Postgres-Service-Container, Fake-Gamma + Fake-CLOB).

**Single-Operator-Audit-Pattern:** GitHub erlaubt kein Self-Approval, daher steht Branch-Protection auf `required_approving_review_count: 0`. Risk-sensitive PRs (Touches `src/risk/**`, `MAX_CAPITAL_EUR` oder `TRADING_MODE`-Flip) verlangen einen `AUDIT_LOG.md`-Eintrag, der den Safety-Review des Operators dokumentiert (was ändert sich, was kann schiefgehen, warum trotzdem sicher). Der Audit-Log ist der Second-Review-Trail; CI kann ihn nicht erzwingen, Operator-Disziplin schon.

**Exit:** Erster trivialer PR gemerged, alle Gates grün, AUDIT_LOG-Initialeintrag.

---

## Phase 2 — Claude-Code-Config-Layer (Dev-Session Hooks + Subagent Definitions)

**Ziel:** `.claude/`-Layer aufgesetzt: Dev-Session-Hooks, defensive Pydantic-Hooks für `Task`/`Agent`-Tool-Use, Subagent-Definitionen für `trading-agent` und `risk-execution` — Pydantic-Hooks teilweise Stubs, weil Models erst in Phase 3 existieren.

**Files:**
- `.claude/settings.json`: registriert die Hooks aus specs/engineering.md §9 mit Pfaden zu `.claude/hooks/*.py`.
- `.claude/hooks/post_tool_use_edit.py` — ruff/mypy/pytest. Phase 2: nur ruff aktiv. Phase 3: scharf.
- `.claude/hooks/pre_tool_use_bash.py` — blockt `rm -rf`, `git push --force`, `git reset --hard`, `.env*`-Writes. Sofort scharf.
- `.claude/hooks/pre_tool_use_risk_edit.py` — blockt Edits unter `src/risk/**` außerhalb Plan-Mode. Sofort scharf.
- `.claude/hooks/stop_gitleaks.py` — gitleaks auf staged diff. Sofort scharf.
- `.claude/hooks/user_prompt_submit_realmoney.py` — Confirmation-Banner bei "live trade"/"echtes Kapital"/"real money". Sofort scharf.
- `.claude/hooks/task_created_validate.py` — **Stub mit `try/except ImportError: sys.exit(0)`** in Phase 2; scharf in Phase 3 (Pydantic-Validate gegen `shared.models.tasks`).
- `.claude/hooks/task_completed_validate.py` — **Stub** in Phase 2; scharf in Phase 3.
- `.claude/agents/trading-agent.md` — Skelett.
- `.claude/agents/risk-execution.md` — Skelett.

**Lösung der Phase-2/3-Zirkularität:** Hook-Stubs für `task_created_validate` / `task_completed_validate` haben in Phase 2 einen `try: from shared.models import ...; except ImportError: sys.exit(0)`-Wrapper. Sobald `src/shared/models/` in Phase 3 existiert, wird der Wrapper als letzter Phase-3-Step durch echte Validation ersetzt. Keine separate „Hook-Aktivierungs-Phase" nötig.

**Agent-Skelette vs. Stream D:** Skelette in Phase 2 enthalten Rolle + Tool-Allow-List + I/O-Beschreibung als Strings (kein Runtime-Import). Stream D in Phase 4 verfeinert die System-Prompts und ergänzt verbose Strategy-Doctrine. Keine Konflikte mit anderen Streams.

**Exit:** `.claude/`-Config erkennt Hook-Skripte (`claude /hooks list`); kein Hook blockt Standard-Edit-/Bash-Workflows beim Smoketest.

---

## Phase 3 — Foundation-Code (sequenziell, blockt Phase 4)

**Ziel:** Alle Schnittstellen einfrieren, die Phase 4 parallel konsumiert. Kein Adapter-Code, keine Skripte, kein Lead-Wiring.

**Files (in dieser Reihenfolge committed):**

1. `src/shared/config/settings.py` — Pydantic-Settings (Single Source of Truth, alle Tunables aus specs/engineering.md §10): `TRADING_MODE` (Default `paper`), `MAX_CAPITAL_EUR` (Default `0`), `EDGE_THRESHOLD=0.03`, `CYCLE_PERIOD_MIN=30`, `TOP_K_MARKETS=50`, `CONCENTRATION_CAP=0.15`, `CYCLE_CAP=0.25`, `LESSONS_TOP_K`, `LESSONS_LOOKBACK_DAYS`, `SURPRISE_THRESHOLD`, `WEB_SEARCH_TIMEOUT`, `AGENT_TIMEOUT`, `MARKET_SNAPSHOTS_RETENTION_DAYS=30`, `INFERENCE_LOG_RETENTION_DAYS=90`, `KEY_PROVIDER` (Default `"encrypted_file"`), `RECONCILIATION_DIFF_USD=0.50`, `SystemStateKey: Literal[...]`-Typ.
2. `src/shared/models/__init__.py` und Pydantic-Modelle: `Market`, `Orderbook`, `MarketMetadata`, `Resolution`, `Position`, `CashBalance`, `Order`, `OrderResult`, `CancelResult`, `OrderStatus`, `Universe`, `PortfolioState`, `Prediction`, `Decision`, `Trade`, `CyclePlan`, `Note`, `Lesson`, `GateResult`.
3. `src/shared/db.py` — `get_session(role: Literal["trading_cycle","outcome_ingestion","lessons_summary"]) -> Session`. Verbindungs-Pool, env-driven `DATABASE_URL_<ROLE>`.
4. `src/shared/adapters/prediction_market.py` — `PredictionMarketAdapter` Protocol (read + write + EIP-712-Signing). **Keine Impl.**
5. `src/shared/adapters/key_provider.py` — `KeyProvider` Protocol. **Keine Impl.**
6. `src/risk/limits.py` — `CONCENTRATION_CAP`, `CYCLE_CAP`, Sanity-Limits aus specs/engineering.md §2.
7. `src/risk/concentration_gate.py`, `src/risk/solvency_gate.py`, `src/risk/cycle_cap_gate.py` — drei Gates mit gemeinsamer Signatur `evaluate(state, order) -> GateResult`.
8. `src/risk/capital_gate.py` — `MAX_CAPITAL_EUR: Final = 0`. Hartes 0, nicht `<TBD>`. Semantisch valid: "kein real-money zugelassen". Operator setzt Wert vor Phase 6 via 2-Reviewer-PR + AUDIT_LOG.
9. `src/risk/kill_switch.py` — liest `system_state(key='kill_switch')`.
10. `src/risk/sanity_gates.py` — Order-Size, Price-Range, Order-Rate, Position-Count.
11. `tests/risk/` — hypothesis-Property-Tests; CI-Gate "100% Coverage auf `src/risk/`" wird ab dieser Phase scharf.
12. `tests/adapters/fake_adapter.py` — `FakeAdapter` als Test-Double, beweist Protocol-Vollständigkeit. Genutzt von Phase-3-Konformitätstests und Phase-4 Stream D als Stand-In.
13. `infra/docker-compose.yml` — nur Postgres 16, mit Init-Mount auf `infra/sql/00_roles.sql`.
14. `infra/.claude-code-version` — gepinnte Claude-Code-Version (specs/orchestration.md §5).
15. `infra/sql/00_roles.sql` — `CREATE ROLE`-Statements für `trading_cycle`, `outcome_ingestion`, `lessons_summary` (idempotent via DO-Block).
16. `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial_schema.py` — 11 Tabellen (`markets`, `market_snapshots`, `predictions` mit `inference_log` JSONB, `decisions`, `trades`, `paper_trades`, `positions`, `notes`, `cycle_plan`, `lessons`, `system_state`) + Column-Level GRANTs für die 3 Rollen.
17. `.import-linter.toml` — Layer-Regeln: `src/risk/` darf nichts importieren außer `shared.config` + `shared.models`; `src/execution/` und `src/research/` dürfen Polymarket nur via `shared.adapters` ansprechen.
18. **Hook-Aktivierung:** `task_created_validate.py` und `task_completed_validate.py` werden auf scharf gestellt (Pydantic-Validate gegen `shared.models`).

**Postgres-Roles-Bootstrap-Detail:** Roles + Passwörter werden über `infra/sql/00_roles.sql` als Docker-Init-Mount erstellt (passiert vor Alembic). Alembic verbindet sich mit dem Owner-Role und führt Schema + GRANTs aus. Vorteil: Roles sind stabile Infrastruktur, GRANTs sind reversible Migrations.

**Eingefrorene Schnittstellen (Hand-off zu Phase 4):**

| # | Schnittstelle | Pfad | Konsumenten |
|---|---|---|---|
| 1 | `Settings`-Klasse | `src/shared/config/settings.py` | A, B, C, D |
| 2 | `PredictionMarketAdapter` Protocol + DTOs | `src/shared/adapters/prediction_market.py` + `src/shared/models/` | A (impl), D (consume) |
| 3 | `KeyProvider` Protocol | `src/shared/adapters/key_provider.py` | A (impl) |
| 4 | Pydantic-Artefakte | `src/shared/models/*` | A, B, C, D |
| 5 | Risk-Gate-Signaturen | `src/risk/*.py` | D (risk-execution member) |
| 6 | Postgres-Schema + Roles + GRANTs | `alembic/versions/0001_initial_schema.py` + `infra/sql/00_roles.sql` | B, C, D |
| 7 | `src/shared/db.py` `get_session(role)` | `src/shared/db.py` | B, C, D |
| 8 | `system_state`-Key-Konvention (Literal-Typ) | `src/shared/config/settings.py` | B, C, D |

**Exit:** `src/risk/`-Coverage = 100%, `alembic upgrade head` + `alembic downgrade base` laufen sauber, FakeAdapter-Konformitätstest grün, `import-linter` greift, alle Hooks scharf.

---

## Phase 4 — 4 PARALLELE Streams

**Synchronisations-Punkt:** Phase 3 in `main` gemerged, alle 8 Schnittstellen API-stable. Empfohlene Aufteilung: vier Worktrees oder vier Claude-Code-Agent-Sessions parallel, jeweils auf eigenem Feature-Branch.

### Stream A — Adapter-Implementierungen + web_search-Skill

**Files:**
- `src/shared/adapters/polymarket.py` — Wrapper um `py-clob-client`, WebSocket-Subscriptions auf getrackte Märkte, REST für Orders, EIP-712-Signing über `KeyProvider`.
- `src/shared/adapters/paper_trading.py` — delegiert Read-Pfade an `PolymarketAdapter`, redirected `place_order`/`cancel_order` in `paper_trades`-Tabelle.
- `src/shared/adapters/key_provider_localfile.py` — encrypted-at-rest Key, Passphrase-Prompt beim Process-Start.
- `src/shared/adapters/factory.py` — wählt Adapter + KeyProvider anhand `Settings`.
- `src/research/skills/web_search.py` — OpenAI Responses API + `web_search_preview`.

**Konsumiert:** Schnittstellen 1, 2, 3, 4.
**Tests:** Unit gegen Mock-CLOB + Mock-KMS-API; EIP-712-Signing-Roundtrip mit Test-Vector; web_search gegen aufgezeichneten OpenAI-Response. Sandbox-Smoke ist **manuell außerhalb CI**.

### Stream B — outcome_ingestion.py

**Files:**
- `src/execution/outcome_ingestion.py` — deterministisches Skript, kein LLM, eigener read-only Gamma-Client.
- `src/execution/outcome_math.py` — PnL, Mark-to-Market (bid-based, conservative).
- `src/execution/reconcile.py` — Diff > $0.50 → Flag (`reconciliation_flag`-Column oder `system_state`-Row).
- `tests/execution/test_outcome_ingestion.py`.

**Konsumiert:** Schnittstellen 1, 4 (`Resolution`, `Trade`, `Position`), 6 (`outcome_ingestion`-Role mit Column-Level UPDATE), 7, 8 (`last_outcome_ingestion_at`).
**Authority Boundary:** **eigener** Gamma-Client (read-only) — **kein** `PolymarketAdapter`-Use. Keine Signing-Keys.
**Tests:** Idempotenz nach partial-failure, high-water-mark-Logik, Reconciliation-Diff-Flagging gegen `FakeGammaAPI`-Fixture.

### Stream C — lessons_summary.py

**Files:**
- `src/research/skills/lessons_summary.py` — daily script, Surprise-Heuristik, INSERT-only auf `lessons`.
- `src/research/surprise_heuristic.py` — `|p_consensus − outcome_as_int| > SURPRISE_THRESHOLD OR realized_pnl outside expected band`.
- `tests/research/test_lessons_summary.py`.

**Konsumiert:** Schnittstellen 1 (`SURPRISE_THRESHOLD`, `LESSONS_LOOKBACK_DAYS`), 4 (`Lesson`-Model), 6 (`lessons_summary`-Role mit nur INSERT auf `lessons`), 7, 8.
**Tests:** Idempotenz auf `(prediction_id, day)`, Heuristik-Property-Test, Template-Determinismus (kein LLM-Output).

### Stream D — Trading-Cycle-Wiring

**Files:**
- `src/execution/lead_bootstrap.py` — Lead-Logik: `_python_scanner` ausführen, Members als headless `claude -p`-Subprocesses spawnen (`execution.subagent_runner`), prev `cycle_plan` lesen, neue `cycle_plan`-Row schreiben.
- `src/execution/cycle_plan.py` — deterministische Synthese (kein LLM-Call).
- `src/execution/notes_tool.py` — `manage_notes` Tool für trading-agent (read/write/edit, LRU ≤ 50).
- `src/research/prompts/trading_agent.md` — Strategy-Doctrine (Mispricing, Edge, web_search-Tool-Use), Step-by-Step-Protocol.
- `src/research/prompts/scanner_reviewer.md`, `src/research/prompts/risk_execution.md`.
- **Verfeinerung** der Skelette in `.claude/agents/*.md` aus Phase 2 (System-Prompts vertiefen, Tool-Allow-Lists final).

**Konsumiert:** Schnittstellen 1, 2 (Adapter via Factory — bis Stream A fertig ist über `FakeAdapter`), 4, 5 (Risk-Gates), 6, 7, 8.
**Soft-Dependency auf Stream A:** Stream D arbeitet bis zum Ende gegen `FakeAdapter` aus Phase 3; Switch auf echten Adapter ist 1-Zeiler in `factory.py` in Phase 5.
**Tests:** Integration mit FakeAdapter; cycle_plan-Schreibe-Test; Pydantic-Artifact-Validation an Hook-Boundaries; **`test_default_trading_mode_is_paper`** (empty-env → `PaperTradingAdapter` → schreibt nach `paper_trades`).

**Exit Phase 4:** Alle 4 Streams gemerged. CI grün. Coverage: `src/risk/` 100%, `src/execution/` ≥90%, Rest ≥80%.

---

## Phase 5 — Integration

- `infra/cron/trading_cycle.cron`, `infra/cron/outcome_ingestion.cron`, `infra/cron/lessons_summary.cron`.
- `infra/scripts/run_cycle.sh` — `timeout 1800`-Wrapper, `.env`-Load, dispatches `trading_cycle` → `uv run python -m execution.run_cycle` (analog für `outcome_ingestion` und `lessons_summary`).
- `tests/e2e/test_paper_cycle.py` — Postgres-Service-Container, FakeGamma + FakeCLOB, ein vollständiger Cycle, Assertions auf alle 11 Tabellen.
- `structlog`-Wiring in alle drei Prozesse mit den per-line-Pflichtfeldern aus specs/data_infrastructure.md §3.
- Optional: minimaler Prometheus-Exporter (`cycle_duration_seconds`, `decisions_total`, `trades_total`, `errors_total`, `equity_usd`, `kill_switch_active`).

**Exit:** E2E-Test grün, alle drei Cron-Jobs lokal lauffähig, AUDIT_LOG enthält Phase-Abschluss-Eintrag, `MAX_CAPITAL_EUR=0`.

---

## Phase 6 — First Live Paper Cycle

- Echter Cron-Tick gegen reale Polymarket-Read-API + `PaperTradingAdapter`.
- Manueller py-clob-client-Sandbox-Smoke einmalig vor diesem Phase-Eintritt.
- Beobachtung: structured-logs, Postgres-Inhalt nach 24h, dann nach 7d, dann nach 30d.
- Kein Code-Output. Erkenntnisse → `AUDIT_LOG.md` und `docs/operations/`.
- **Voraussetzung für eine Phase 7 (jemals real_capital):** ≥30d in paper, `MAX_CAPITAL_EUR` per 2-Reviewer-PR auf > 0 gesetzt, KMS-Setup live (oder bewusst encrypted-file weiterführen mit dokumentiertem Risiko).

---

## Verifizierungs-Checks (am Ende jeder Phase)

- **Phase 0:** `uv sync && uv run ruff check . && uv run mypy --strict .` grün auf leerem Repo; pre-commit-Hooks aktiv.
- **Phase 1:** Trivial-PR mit ADR mergt grün; Branch-Protection blockt direkten `main`-Push (Test); CODEOWNERS funktioniert (Test-PR auf `src/risk/`-Datei verlangt 2 Reviews).
- **Phase 2:** Hooks aus `.claude/settings.json` sind durch `claude /hooks list` sichtbar; Smoketest auf `Bash`-Block (rm -rf), `Edit`-RiskGate (`src/risk/` ohne Plan-Mode) und `Stop`-Gitleaks (eingefügtes Test-Secret) zeigt Verhalten wie spezifiziert.
- **Phase 3:** `pytest --cov=risk --cov-fail-under=100`, `alembic upgrade head && alembic downgrade base`, Protocol-Konformitätstest gegen FakeAdapter, `lint-imports` grün.
- **Phase 4:** Alle Streams CI-grün; `test_default_trading_mode_is_paper` grün; Cross-Stream-Smoketest (Stream D + FakeAdapter; Stream B liest, was Stream D schrieb; Stream C liest, was Stream B schrieb).
- **Phase 5:** `pytest tests/e2e/test_paper_cycle.py` grün; lokaler Cron-Tick erzeugt Rows in `predictions`/`decisions`/`paper_trades`/`cycle_plan`.
- **Phase 6:** Erste echte Paper-Cycle schreibt in `paper_trades` (nicht `trades`); `MAX_CAPITAL_EUR=0` blockiert versehentliche real_capital-Order; Kill-Switch-Toggle stoppt neue Orders im nächsten Cycle.

---

## Schnittstellen-Liste (Quick-Reference)

| # | Was | Wo | Stream-Konsumenten |
|---|---|---|---|
| 1 | Settings | `src/shared/config/settings.py` | A B C D |
| 2 | `PredictionMarketAdapter` Protocol | `src/shared/adapters/prediction_market.py` | A (impl) D |
| 3 | `KeyProvider` Protocol | `src/shared/adapters/key_provider.py` | A (impl) |
| 4 | Pydantic Models | `src/shared/models/` | A B C D |
| 5 | Risk Gates Signaturen | `src/risk/*.py` | D |
| 6 | Schema + Roles + GRANTs | `alembic/0001_*.py` + `infra/sql/00_roles.sql` | B C D |
| 7 | DB-Session-Helper | `src/shared/db.py` | B C D |
| 8 | system_state Keys | Literal in `src/shared/config/settings.py` | B C D |

---

## Risiken & offene Punkte

- **EIP-712-Roundtrip vor erstem real_capital-Cycle ungetestet außer Mock.** Phase 6-Eintrittsbedingung: einmaliger manueller Sandbox-Roundtrip mit Test-Key.
- **Single-Operator-Audit-Pattern** ist organisatorische Disziplin: GitHub kann es nicht erzwingen (Self-Approval verboten, daher 0 Required Approvals); `AUDIT_LOG.md`-Einträge bei `src/risk/**`-, `MAX_CAPITAL_EUR`- oder `TRADING_MODE`-Touches sind die einzige Spur.
- **`MAX_CAPITAL_EUR=0` blockt real_capital-Orders by design.** Operator muss aktiv setzen — Feature, kein Bug.
- **Polymarket-API-Versions-Drift:** `py-clob-client`-Pin in `pyproject.toml` essenziell; manueller Smoke vor jedem Bump.
- **TIF=FAK ist hardcoded** im `PolymarketAdapter` (`specs/data_infrastructure.md §2`). Beim ersten `real_capital`-Sandbox-Smoke verifizieren, dass eine künstlich-zu-große Order tatsächlich nur einen Partial-Fill produziert und keine Resting-Order auf dem Buch liegen lässt (`get_open_orders()` muss leer sein).
