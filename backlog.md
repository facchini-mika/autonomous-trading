# Backlog — Polymarket Autonomous Trading

**Stand:** 2026-05-04
**Quelle:** Diff der 7 Spec-Files (`specs/specs.md`, `trading.md`, `engineering.md`, `data_infrastructure.md`, `orchestration.md`, `trading_feedback.md`, `optimization.md`) und `plan.md` gegen Code-Stand auf `main` (`b6ee387`).
**Bereits implementiert (NICHT im Backlog):** Alle MVP-Items aus Phasen 0–6c (siehe `AUDIT_LOG.md`).

## Ranking-Methodik

Sortierung nach Operator-Wert vor erstem `real_capital`-Cycle:

| Tier | Thema | Begründung |
|------|-------|-----------|
| **P0** | Reliability / Safety | Vorbedingung für `real_capital > 0` — wenn das fehlt, kann kein verantwortbarer Live-Cycle laufen. |
| **P1** | Observability | Operator-Vertrauen — ohne Sichtbarkeit kein bewusstes Hochfahren. |
| **P2** | Auto-Learning (Tier-1+2 Feedback) | Skaliert das System, ohne dass Operator-Zeit linear mitwächst. |
| **P3** | Trading-Edge | Strategy-Tiefe — sobald Pipeline + Feedback stabil, Edge maximieren. |
| **P4** | Infra-Hardening | Compliance / Skalierungsdruck — getrieben durch Kapital-Größe. |
| **P5** | Skalierung & Forschung | Erst nach erwiesener Profitabilität sinnvoll. |

Innerhalb jedes Tiers: `[Spec-Pflicht]` (in MVP-Specs angekündigt, später deferred) vor `[Optimierung]` (Post-MVP-Vision).

Aufwand-Schätzung: **S** ≤ 1 Tag, **M** 2–5 Tage, **L** ≥ 1 Woche.

---

## P0 — Reliability / Safety

### P0.1 Safety-Watchdog-Service [Spec-Pflicht]
- **Spec:** `orchestration.md §7`, `data_infrastructure.md §2`
- **Was:** Eigenständiger Python-Daemon, beobachtet trading_cycle/outcome_ingestion/lessons_summary; ≥3 consecutive failures → `system_state.kill_switch=true` + page.
- **Warum wichtig:** Heute kann ein gefrorener Cycle still scheitern; Watchdog ist die einzige automatisierte Selbstheilung neben dem manuellen Kill-Switch.
- **Aufwand:** M. **Vorbedingung:** keine.

### P0.2 Drawdown Trip-Wires [Spec-Pflicht]
- **Spec:** `data_infrastructure.md §3`, `engineering.md §13`
- **Was:** Equity-Tracker, der bei Drawdown >10% warnt und >15% den Kill-Switch automatisch zieht.
- **Warum wichtig:** Schützt vor Tail-Loss in Live-Cycles, ohne dass Operator wach ist.
- **Aufwand:** S. **Vorbedingung:** Equity-Snapshot pro Cycle.

### P0.3 30s Reconciler-Loop als Service [Spec-Pflicht]
- **Spec:** `orchestration.md §7`, `data_infrastructure.md §2`
- **Was:** Heutiger one-shot `reconcile.py` wird zum Long-running Service mit 30s-Cadence; Diff > $10/total → freeze + page (heute Flag, kein Auto-Stop).
- **Warum wichtig:** Mismatch zwischen interner Buchung und Broker-Truth ist das wahrscheinlichste Live-Issue.
- **Aufwand:** M. **Vorbedingung:** P0.1.

### P0.4 Polymarket Circuit-Breaker verifizieren / nachziehen [Spec-Pflicht]
- **Spec:** `data_infrastructure.md §2`
- **Was:** 5 errors / 60s → 5 min cooldown im PolymarketAdapter. Spec sagt MVP, Code-Status nicht sicher verifiziert.
- **Warum wichtig:** Verhindert Order-Storm bei API-Flapping.
- **Aufwand:** S. **Vorbedingung:** keine.

### P0.5 Alertmanager-Wiring [Spec-Pflicht]
- **Spec:** `data_infrastructure.md §3`
- **Was:** Alert-Pipeline (Pushover/Email/Slack) für: kill_switch event (info), drawdown warn/critical, cycle latency warn, reconciliation diff critical, error rate warn.
- **Warum wichtig:** Trip-Wires ohne Alert sind Blindgänger.
- **Aufwand:** S. **Vorbedingung:** P0.2.

### P0.6 Sandbox-Smoke-Test als Pre-Cycle-Check [Optimierung]
- **Spec:** `engineering.md §7`, `plan.md Phase 6`
- **Was:** Automatisierter EIP-712-Roundtrip gegen Polymarket-Sandbox vor jedem Cron-Tick im real_capital-Modus.
- **Warum wichtig:** Schlüssel/Signing-Drift sofort erkennen, nicht erst beim ersten echten Order-Placement.
- **Aufwand:** S. **Vorbedingung:** Sandbox-API verfügbar.

---

## P1 — Observability

### P1.1 Prometheus-Exporter [Spec-Pflicht]
- **Spec:** `data_infrastructure.md §3`
- **Was:** `cycle_duration_seconds` (Histogram), `decisions_total`, `trades_total`, `errors_total`, `equity_usd`, `gross_exposure_usd`, `drawdown_pct`, `kill_switch_active`.
- **Warum wichtig:** Voraussetzung für jeden Alert/Dashboard; ohne Metriken kein operativer Überblick.
- **Aufwand:** S. **Vorbedingung:** keine.

### P1.2 Real-time Operator-Dashboard [Spec-Pflicht]
- **Spec:** `trading_feedback.md §6`
- **Was:** Lokales Web-UI (Streamlit/Grafana) mit Live-PnL, Position-State, recent trades, last cycle status.
- **Warum wichtig:** Operator muss in Sekunden sehen, was läuft — keine `psql`-Queries unter Stress.
- **Aufwand:** M. **Vorbedingung:** P1.1.

### P1.3 Daily PnL Email [Spec-Pflicht]
- **Spec:** `trading_feedback.md §6`
- **Was:** Cron-getriggerter HTML-Mailer mit equity, realized PnL, open positions, lessons.
- **Warum wichtig:** Passive Sichtbarkeit ohne dass Operator aktiv hinschauen muss.
- **Aufwand:** S. **Vorbedingung:** P1.1 oder direkter DB-Read.

### P1.4 OpenTelemetry-Tracing [Optimierung]
- **Spec:** `data_infrastructure.md §3`
- **Was:** Ein Trace pro Decision-Cycle, Spans pro Service-Call (web_search, gamma, polymarket, gates).
- **Warum wichtig:** Macht Cycle-Latency- und Tool-Use-Pattern debugbar.
- **Aufwand:** M. **Vorbedingung:** P1.1.

### P1.5 Grafana Dashboards [Optimierung]
- **Spec:** `data_infrastructure.md §3`
- **Was:** 4 Dashboards: system health, capital+exposure, per-agent performance, trade flow heatmap.
- **Warum wichtig:** Visuelle Pattern-Erkennung über Tage/Wochen.
- **Aufwand:** M. **Vorbedingung:** P1.1, P2.3.

### P1.6 Weekly Performance Report [Optimierung]
- **Spec:** `trading_feedback.md §6`
- **Was:** Wöchentliche Auto-Doku mit per-agent attribution, hit_rate, sharpe, drawdown.
- **Warum wichtig:** Input für Code-Eval-Team-Reviews (P2).
- **Aufwand:** S. **Vorbedingung:** P2.3.

### P1.7 Monthly Deep-Dive Auto-Report [Optimierung]
- **Spec:** `trading_feedback.md §6`
- **Was:** Strategy/roster review-Briefing (Operator-eingespeist in monatliches Code-Eval-Team).
- **Warum wichtig:** Strukturiert die monatliche Optimierungs-Iteration.
- **Aufwand:** S. **Vorbedingung:** P1.6, P2.7.

---

## P2 — Auto-Learning (Tier-1 + Tier-2 Feedback)

### P2.1 Trade Evaluation Team (Tier 1) [Spec-Pflicht]
- **Spec:** `trading_feedback.md §6`, `orchestration.md §7`
- **Was:** Claude Code Agent Team mit evaluator + 3 Subagents (outcome-fetcher, pnl-aggregator, agent-performance-updater); ~1-min cron.
- **Warum wichtig:** Ersetzt manuelle Operator-Review durch automatisierte Tier-1-Auswertung; Voraussetzung für jede Per-Agent-Optimierung.
- **Aufwand:** L. **Vorbedingung:** P2.3 (Tabelle), ≥30d paper.

### P2.2 Code Evaluation Team (Tier 2) [Spec-Pflicht]
- **Spec:** `optimization.md §5`, `orchestration.md §7`
- **Was:** 6-Rollen-Team (risk-auditor, pattern-miner, strategy-optimizer, strategy-explorer, prior-art-scout, meta-reviewer); daily/weekly/monthly batches.
- **Warum wichtig:** Der eigentliche Auto-Learning-Loop — proposes Code-/Prompt-/Limit-Diffs, die Operator als PR mergt.
- **Aufwand:** L. **Vorbedingung:** P2.1, P2.4, P2.6.

### P2.3 `agent_performance`-Tabelle + Per-Agent Attribution [Spec-Pflicht]
- **Spec:** `trading_feedback.md §6`, `data_infrastructure.md §1`
- **Was:** Alembic-Migration; Felder `hit_rate_30d`, `sharpe_30d`, `pnl_30d`, `pairwise_corr_30d`; Updates durch Tier-1.
- **Warum wichtig:** Ohne Per-Agent-Metriken keine Strategy-Optimierung.
- **Aufwand:** S. **Vorbedingung:** keine.

### P2.4 Pattern-Miner [Spec-Pflicht]
- **Spec:** `optimization.md §5`
- **Was:** Wöchentliches Clustering der `lessons` → `patterns`-Tabelle (mit Alembic-Migration).
- **Warum wichtig:** Aggregiert Single-Lessons zu wiederkehrenden Mustern (z.B. „microstructure bias auf NFL-Märkten unter X liquidity").
- **Aufwand:** M. **Vorbedingung:** ≥90d lessons-Daten, P2.3.

### P2.5 Pairwise-Correlation Tracking [Spec-Pflicht]
- **Spec:** `trading_feedback.md §6`
- **Was:** Rolling 30d-Korrelation der Per-Agent PnL-Streams; Input für strategy-optimizer (redundante Agents retiren).
- **Warum wichtig:** Verhindert, dass das Ensemble nur größer, aber nicht informativer wird.
- **Aufwand:** S. **Vorbedingung:** P2.3.

### P2.6 Fixed Explore/Exploit-Allocator + Anti-Whipsaw [Spec-Pflicht]
- **Spec:** `optimization.md §5`
- **Was:** Per top-K=5 wöchentlich ≥1 explore + ≥2 exploit; ≥5–7d min live, bevor Strategy ersetzt wird; Counter-Rules bei drawdown / green weeks.
- **Warum wichtig:** Strukturiert Tier-2-Decisions; verhindert Whipsaw.
- **Aufwand:** S. **Vorbedingung:** P2.2.

### P2.7 Monthly Retrospective (Lessons-Compaction) [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Lessons >90d werden zu per-category LTKDs (Long-Term Knowledge Documents) komprimiert; stale/contradicted gepruned.
- **Warum wichtig:** Hält Lessons-Top-K-Injection (heute MVP-Feature) langfristig kuratiert statt zugemüllt.
- **Aufwand:** M. **Vorbedingung:** ≥6 Monate lessons-Daten.

### P2.8 `proposals`-Tabelle (Code-Eval-Team-Output) [Spec-Pflicht]
- **Spec:** `data_infrastructure.md §1`, `optimization.md §5`
- **Was:** Persistierte Strategy-Optimizer-/Explorer-Vorschläge mit Status (open/accepted/rejected/merged).
- **Warum wichtig:** Audit-Trail für jede Tier-2-Empfehlung.
- **Aufwand:** S. **Vorbedingung:** P2.2.

---

## P3 — Trading-Edge

### P3.1 7-Persona-Ensemble [Spec-Pflicht]
- **Spec:** `trading.md §8`
- **Was:** base-rate-Bayesian, news-synthesizer, domain-router, historical-analogue, contrarian, microstructure, red-team — als parallele Subagents im trading-cycle.
- **Warum wichtig:** Zentrale Edge-Quelle laut Spec; heute existiert nur ein einzelner trading-agent.
- **Aufwand:** L. **Vorbedingung:** P2.3 (Per-Agent-Attribution), Operator-OK für Anthropic-Spend-Anstieg.

### P3.2 Beliefs + Position-Thesis [Spec-Pflicht]
- **Spec:** `trading.md §8`, `data_infrastructure.md §1`
- **Was:** Typed structured views mit Revisions-Lineage; `beliefs`-Tabelle.
- **Warum wichtig:** Macht Mental-Models über Cycles persistent statt nur per-Cycle ad-hoc.
- **Aufwand:** M. **Vorbedingung:** keine.

### P3.3 Operating-Doctrine [Spec-Pflicht]
- **Spec:** `trading.md §8`, `optimization.md §5`, `data_infrastructure.md §1`
- **Was:** Persistente Multi-Phase-Strategy mit entry/exit conditions; `operating_doctrine`-Tabelle; aktualisiert durch Code-Eval-Team.
- **Warum wichtig:** Macht Strategy explizit reviewable statt implicit-im-Prompt.
- **Aufwand:** M. **Vorbedingung:** P2.2.

### P3.4 Strategy-Skill-Library (Voyager-Pattern) [Spec-Pflicht]
- **Spec:** `trading.md §8`
- **Was:** Named procedural helpers (`src/research/skills/index.toml`); Versioning, Discovery via Skill-Tool.
- **Warum wichtig:** Verhindert Re-Invention pro Cycle; ermöglicht Skill-Komposition.
- **Aufwand:** M. **Vorbedingung:** keine.

### P3.5 Multi-Strategy-Lifecycle [Spec-Pflicht]
- **Spec:** `trading.md §8`, `optimization.md §5`
- **Was:** Per-Strategy PnL, Capital-Allocation, Anti-Whipsaw-Block (≥5–7d min live).
- **Warum wichtig:** Voraussetzung für mehr als nur Mispricing-Single-Strategy.
- **Aufwand:** M. **Vorbedingung:** P2.3, P2.6.

### P3.6 Richer Research Tools [Optimierung]
- **Spec:** `trading.md §8`
- **Was:** `news_fetch`, `historical_analogue_lookup`, `related_market_scan` als zusätzliche Skills.
- **Warum wichtig:** web_search alleine ist begrenzt; spezialisierte Tools liefern strukturiertere Signale.
- **Aufwand:** M (pro Tool S). **Vorbedingung:** P3.4.

### P3.7 No-Filter Universe + Ensemble-Triage [Optimierung]
- **Spec:** `trading.md §8`
- **Was:** Top-K-Filter (heute K=50) wegfallen lassen; Ensemble triagiert das gesamte Polymarket-Universum.
- **Warum wichtig:** Mispricing-Detection auf illiquiden Märkten oft profitabler.
- **Aufwand:** L. **Vorbedingung:** P3.1, deutlich höheres Token-Budget.

### P3.8 Recent-Settlement Prompt Windows [Optimierung]
- **Spec:** `trading.md §8`
- **Was:** Last-N resolved markets + closed trades als Pflicht-Kontext im trading-agent-Prompt.
- **Warum wichtig:** Macht jüngste Outcomes/Misses sichtbar im Decision-Moment.
- **Aufwand:** S. **Vorbedingung:** keine.

### P3.9 Sub-Second Snapshot Loop für Active Positions [Optimierung]
- **Spec:** `trading.md §8`, `orchestration.md §7`
- **Was:** 1-Sekunden-Cadence für Orderbooks von Märkten, in denen offene Positionen liegen.
- **Warum wichtig:** Tighter Stops/Take-Profit auf liquiden Märkten.
- **Aufwand:** M. **Vorbedingung:** P4.2 (TimescaleDB) oder Volume-Pruning.

---

## P4 — Infra-Hardening

### P4.1 AWS KMS Integration [Spec-Pflicht]
- **Spec:** `engineering.md §7`, `engineering.md §13`
- **Was:** `KeyProvider`-Implementierung gegen AWS KMS (statt encrypted-file).
- **Warum wichtig:** Bei real_capital >> 0 ist encrypted-file ein dokumentiertes Risiko.
- **Aufwand:** M. **Vorbedingung:** AWS-Konto, Operator-Entscheidung „lokal vs. cloud".

### P4.2 TimescaleDB Hypertables [Optimierung]
- **Spec:** `engineering.md §13`, `data_infrastructure.md`
- **Was:** Migration `market_snapshots` und `inference_log` auf Hypertables.
- **Warum wichtig:** Sub-second snapshots (P3.9) erzeugen Volumen, das vanilla Postgres nicht hält.
- **Aufwand:** M. **Vorbedingung:** P3.9-Druck.

### P4.3 AWS Secrets Manager / Vault [Optimierung]
- **Spec:** `engineering.md §13`
- **Was:** API-Tokens (OpenAI, Anthropic, Polymarket) zentral verwaltet.
- **Warum wichtig:** Rotation + Audit für Secrets ohne `.env`-Edits.
- **Aufwand:** S. **Vorbedingung:** P4.1 (gleiches Cloud-Setup).

### P4.4 Redis Hot-State + Pub/Sub [Optimierung]
- **Spec:** `engineering.md §13`, `data_infrastructure.md`
- **Was:** Heißer State (open positions, last orderbook) in Redis; pub/sub für event-driven Subagents.
- **Warum wichtig:** Voraussetzung für P5.6 (Real-Time Event-Driven).
- **Aufwand:** M. **Vorbedingung:** P5-Workload.

### P4.5 S3 Cold-Archive [Optimierung]
- **Spec:** `engineering.md §13`, `data_infrastructure.md`
- **Was:** `inference_log` JSONB-Blobs + screenshots nach 90d in S3/Glacier.
- **Warum wichtig:** Langfristige Audit-Replay-Fähigkeit, ohne Postgres aufzublähen.
- **Aufwand:** S. **Vorbedingung:** P4.1.

### P4.6 ECS Fargate / k8s CronJob [Optimierung]
- **Spec:** `engineering.md §13`
- **Was:** Cron + lokales Docker durch Cloud-Scheduler ersetzen.
- **Warum wichtig:** Erst nötig, wenn Local-Setup kein Single-Point-of-Failure mehr sein darf.
- **Aufwand:** L. **Vorbedingung:** P4.1, P4.3.

### P4.7 CloudWatch + Sentry (Lighter Observability) [Optimierung]
- **Spec:** `engineering.md §13`
- **Was:** Alternative zur self-hosted Prometheus+Grafana+Alertmanager-Stack.
- **Warum wichtig:** Reduziert Ops-Overhead, wenn AWS sowieso da ist.
- **Aufwand:** M. **Vorbedingung:** P4.1.

### P4.8 YubiHSM / Hardware-Wallet [Optimierung]
- **Spec:** `engineering.md §13`, `optimization.md §5`
- **Was:** HSM-backed signing für sehr große Kapital-Allokationen.
- **Warum wichtig:** Erst bei Kapital, das KMS-Risiko übersteigt.
- **Aufwand:** L. **Vorbedingung:** Kapital >> KMS-zumutbar.

---

## P5 — Skalierung & Forschung

### P5.1 Subagent Fan-Out (Research-Subagents) [Spec-Pflicht]
- **Spec:** `orchestration.md §7`, `engineering.md §13`
- **Was:** web-searcher, news-fetcher, analogue-finder, related-market-scanner als 1-way parallele Subagents pro Persona.
- **Warum wichtig:** Senkt Latency und sammelt mehr Evidence pro Cycle.
- **Aufwand:** M. **Vorbedingung:** P3.1.

### P5.2 24h Capital-Allocation-Rebalance [Spec-Pflicht]
- **Spec:** `orchestration.md §7`
- **Was:** Meta-Allocator-Service, der wöchentlich/täglich Kapital zwischen Strategien re-allokiert.
- **Warum wichtig:** Macht Per-Strategy-Lifecycle (P3.5) effektiv.
- **Aufwand:** M. **Vorbedingung:** P3.5, ≥30d Multi-Strategy live.

### P5.3 Multi-Cadence Loops [Spec-Pflicht]
- **Spec:** `orchestration.md §7`, `trading.md §8`
- **Was:** 1s snapshots / 30s reconciler / 24h rebalance / hourly+daily+weekly batches als Service-Mesh.
- **Warum wichtig:** Aggregiert P0.3, P3.9, P5.2 in eine kohärente Scheduler-Schicht.
- **Aufwand:** L. **Vorbedingung:** P0.3, P3.9, P5.2.

### P5.4 KalshiAdapter (Read-Only) [Optimierung]
- **Spec:** `data_infrastructure.md §2`, `optimization.md §5`
- **Was:** Cross-Venue-Reference; same-event Markets auf Polymarket vs. Kalshi.
- **Warum wichtig:** Implizite Mispricing-Signale durch Inter-Venue-Spreads.
- **Aufwand:** M. **Vorbedingung:** keine.

### P5.5 Cross-Venue Arbitrage Engine [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Statistical-Arb auf Kalshi + Sportsbooks; eigene Strategy.
- **Warum wichtig:** Marktneutrale Edge, weniger Edge-Decay.
- **Aufwand:** L. **Vorbedingung:** P5.4, regulatorischer Check.

### P5.6 Real-Time Event-Driven Pipeline [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Sub-Second News → Trade Pipeline (Webhooks/WS-Streams statt Cron).
- **Warum wichtig:** Erfasst News-Edges, die im 12-min-Cycle verpufft sind.
- **Aufwand:** L. **Vorbedingung:** P4.4, P5.3.

### P5.7 RL Fine-Tuning per Agent [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Per-Agent-Modellvarianten, trainiert auf resolved predictions mit PnL-Reward.
- **Warum wichtig:** Edge-Verschiebung von Prompt-Tuning zu Modell-Tuning.
- **Aufwand:** L. **Vorbedingung:** Anthropic Managed Fine-Tuning verfügbar, ≥6 Mo. Daten.

### P5.8 Prompt-Optimization (DSPy / TextGrad) [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Automatisierte Prompt-Suche mit historical PnL/hit-rate als Loss.
- **Warum wichtig:** Schneller als manuelle Prompt-Iteration durch Code-Eval-Team.
- **Aufwand:** M. **Vorbedingung:** P2.3, ≥3 Mo. Daten.

### P5.9 Multi-Model Ensemble (Sonnet/Haiku) [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Verschiedene Claude-Tiers für verschiedene Personas, sofern Fehler unkorreliert.
- **Warum wichtig:** Diversifiziert Modell-Bias; senkt Cost auf nicht-kritischen Pfaden.
- **Aufwand:** S. **Vorbedingung:** P3.1, gemessene Korrelation.

### P5.10 Scalar / Categorical Markets [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Trading außerhalb binär-resolvierter Märkte (range-, multi-outcome-Märkte).
- **Warum wichtig:** Erweitert TAM auf Polymarket.
- **Aufwand:** L. **Vorbedingung:** Polymarket-API-Support.

### P5.11 Multi-Formal-MAB (f-dsw Thompson Sampling) [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Reward-Attribution per Multi-Armed Bandit, sobald PnL-Feedback schneller wird.
- **Warum wichtig:** Effizienter als fixed Explore/Exploit-Ratio (P2.6).
- **Aufwand:** M. **Vorbedingung:** P2.6 läuft, P5.6 ermöglicht schnellere Feedback-Loops.

### P5.12 ZK Proofs für Provenance [Optimierung]
- **Spec:** `optimization.md §5`
- **Was:** Privacy-/Security-Maturation für Strategie-Provenance.
- **Warum wichtig:** Research-Stage; nur falls Strategie veröffentlicht/verkauft werden soll.
- **Aufwand:** L. **Vorbedingung:** Geschäftsmodell-Entscheidung.

---

## Querschnittsthemen (Backlog-übergreifend)

- **`MAX_CAPITAL_EUR > 0`-Setting**: Kein Backlog-Item, aber Pflicht-Trigger; vorher P0.1, P0.2, P0.5 erledigt + ≥30d paper.
- **Spec-Drift-Audit**: Backlog turnusmäßig (z.B. nach jedem Phase-Abschluss) gegen Specs + Code abgleichen — Items entfernen, die zwischenzeitlich gemerged wurden.
- **AUDIT_LOG-Eintrag** für jede P0-Implementierung verpflichtend (touches `src/risk/**` oder Settings).
