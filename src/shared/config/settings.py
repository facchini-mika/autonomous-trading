"""Single source of truth for all numeric tunables and runtime settings.

Per CLAUDE.md and specs/engineering.md §10, every threshold, limit, and parameter
lives here. Never hardcode values in risk/, execution/, or research/.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, ClassVar, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

TradingMode = Literal["paper", "real_capital"]
KeyProviderKind = Literal["encrypted_file", "aws_kms"]
SystemStateKey = Literal[
    "kill_switch",
    "last_cycle_ts",
    "last_outcome_ingestion_at",
    "last_lessons_summary_at",
    "last_evaluation_at",
    "last_trading_cycle_at",
]


def reconciliation_flag_key(trade_id: str) -> str:
    """Build a `system_state.key` for a per-trade reconciliation flag.

    Reconciliation keys are variadic and cannot be expressed as a Literal;
    consumers should use this helper rather than format the string by hand.
    """
    return f"reconciliation_flag:{trade_id}"


class Settings(BaseSettings):
    """Pydantic-settings root. All values are env-overridable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Mode + capital
    TRADING_MODE: TradingMode = "paper"
    PAPER_STARTING_CASH_USD: float = 10000.0

    # Strategy
    EDGE_THRESHOLD: float = 0.05
    CYCLE_PERIOD_MIN: int = 30
    TOP_K_MARKETS: int = 20
    # Polymarket's `sampling-markets` returns ~1000 markets per page, sorted by
    # internal reward incentive (not TTR or liquidity). Live measurement on
    # 2026-05-19 showed only 27/1000 markets in the TTR=14d window, scattered
    # across positions 7-992 (only 4 in the top-200). The Lead pre-filters by
    # TTR window before fetching orderbooks, so we pull the full page and let
    # the filter cut it down. ``UNIVERSE_FETCH_LIMIT`` caps the post-TTR pool
    # that gets orderbook + metadata fetches (the per-cycle HTTP-call budget).
    UNIVERSE_RAW_FETCH_LIMIT: int = 1000
    UNIVERSE_FETCH_LIMIT: int = 200

    # Universe-selection thresholds (Lead's deterministic Python scanner inputs)
    MIN_DEPTH_1PCT_USD: float = 100.0
    MAX_SPREAD: float = 0.10
    MIN_TIME_TO_RESOLUTION_HOURS: int = 6
    # Aligned with the trading-agent web_search skip threshold in
    # research/prompts/trading_agent.md. Markets resolving > 14 days out are
    # guaranteed to skip web_search (no fresh research) and therefore cannot
    # produce actionable predictions; admitting them wastes scanner budget.
    MAX_TIME_TO_RESOLUTION_DAYS: int = 14
    SOON_RESOLVE_THRESHOLD_DAYS: int = 7
    SOON_RESOLVE_BOOST_MULTIPLIER: float = 1.5

    # Risk caps (decimals, e.g. 0.15 = 15%)
    CONCENTRATION_CAP: float = 0.15
    CYCLE_CAP: float = 0.25

    # Edge-proportional sizing (Phase 6b PR 4)
    BASE_TRADE_FRACTION: float = 0.02
    EDGE_SIZING_SCALE: float = 1.0
    MAX_TRADE_FRACTION: float = 0.10

    # Fee management (Phase 6c — pre-live integration)
    FEE_RATE_BPS: int = 200

    # Lessons / learning
    LESSONS_TOP_K: int = 10
    LESSONS_LOOKBACK_DAYS: int = 30
    SURPRISE_THRESHOLD: float = 0.3

    # Inline feedback phase — closes the trade -> outcome -> lesson -> decision
    # loop inside a single trading_cycle run. Before the trading-agent fires,
    # the lead ingests freshly-resolved outcomes and regenerates lessons so the
    # agent prompt sees the newest evidence. The standalone outcome_ingestion
    # and lessons_summary crons remain as a fallback safety net (idempotent).
    INLINE_FEEDBACK_ENABLED: bool = True
    INLINE_OUTCOME_INGESTION_ENABLED: bool = True
    INLINE_LESSONS_SUMMARY_ENABLED: bool = True
    # Tier-1 LLM eval is expensive (3 subagents) and writes agent_performance
    # rows which are NOT read by the trading-agent — no closed-loop benefit,
    # so default OFF. Enable + gate via the interval if you want it co-located.
    INLINE_TIER1_EVAL_ENABLED: bool = False
    INLINE_TIER1_EVAL_INTERVAL_MIN: int = 60
    # Per-sub-step hard cap. ThreadPoolExecutor.future.result(timeout=…) is
    # used because outcome_ingestion is synchronous httpx; SIGALRM is unsafe
    # in a worker thread / on subprocess hosts.
    INLINE_FEEDBACK_TIMEOUT_SEC: int = 90
    # Inline lookback is short because the high-water-mark is the primary
    # window cursor; this is only the bootstrap floor on first run.
    INLINE_OUTCOME_LOOKBACK_DAYS: int = 1

    # Timeouts
    WEB_SEARCH_TIMEOUT_SEC: int = 120
    # Trading-agent in particular accumulates 2-3 web_searches at 120s each
    # plus inference time. Scanner-reviewer was observed at 600s exactly
    # (cycle-1777990078, 2026-05-05) when UNIVERSE_FETCH_LIMIT was 200;
    # +50% margin keeps Anthropic-latency jitter from kicking the cycle
    # over the cliff. The wrapper script (`infra/scripts/run_cycle.sh`)
    # bounds the whole cycle at 1800s — keep the per-agent cap below the
    # wrapper, well below 3x per-agent so worst-case stage-stacking
    # still hits the safety net.
    AGENT_TIMEOUT_SEC: int = 900

    # Per-subagent USD budget caps passed to ``claude --max-budget-usd``.
    # The CLI aborts the call once the in-flight cost would exceed the cap,
    # so a runaway agent can never burn more than these amounts in one cycle.
    BUDGET_USD_TRADING: float = 5.0
    BUDGET_USD_RISK: float = 1.0

    # Web-search blocklist: hosts (and their subdomains) whose URLs are
    # stripped from web_search hits before they reach the trading-agent.
    # NoDecode opts out of pydantic-settings' default JSON decoding so the
    # env override can be a plain comma-separated string.
    WEB_SEARCH_BLOCKED_DOMAINS: Annotated[list[str], NoDecode] = ["coinmarketcap.com"]

    @field_validator("WEB_SEARCH_BLOCKED_DOMAINS", mode="before")
    @classmethod
    def _split_csv_domains(cls, v: object) -> object:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [s.strip() for s in stripped.split(",") if s.strip()]
        return v

    # Retention
    MARKET_SNAPSHOTS_RETENTION_DAYS: int = 30
    INFERENCE_LOG_RETENTION_DAYS: int = 90
    CYCLE_PLAN_RETENTION_DAYS: int = 90

    # Key management
    KEY_PROVIDER: KeyProviderKind = "encrypted_file"
    KEY_PROVIDER_PATH: Path = Path.home() / ".config" / "polymarket-trading" / "wallet.json"

    # External APIs (Phase 4 Stream A)
    POLYMARKET_HOST: str = "https://clob.polymarket.com"
    POLYGON_CHAIN_ID: int = 137
    POLYGON_RPC_URL: str = "https://polygon-bor-rpc.publicnode.com"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5.5"

    # OpenAI pricing for the Responses-API calls made by the web_search MCP
    # skill. ClassVar so pydantic-settings does not treat these as overridable
    # fields — pricing is a constant, not a tunable, and must not be set via
    # env. Keys are model names (matching ``OPENAI_MODEL``); values are USD
    # rates that ``research.skills.openai_cost.calculate_cost_usd`` consumes.
    #
    # gpt-5.5 rates as of 2026-05-05 from openai.com/api/pricing:
    #   - $5.00 / 1M input tokens
    #   - $30.00 / 1M output tokens
    #   - $0.50 / 1M cached input tokens (90% prompt-cache discount)
    #   - $10.00 / 1k web_search tool invocations
    # Search-content tokens (the page text returned by the web_search tool)
    # are billed at $0 by OpenAI — they are excluded from
    # ``response.usage.input_tokens`` upstream, so the formula below does not
    # need a special case for them.
    OPENAI_PRICING: ClassVar[dict[str, dict[str, Decimal]]] = {
        "gpt-5.5": {
            "input_per_1m_usd": Decimal("5.00"),
            "output_per_1m_usd": Decimal("30.00"),
            "cached_input_per_1m_usd": Decimal("0.50"),
            "web_search_per_1k_usd": Decimal("10.00"),
        },
    }

    # Reconciliation
    RECONCILIATION_DIFF_USD: float = 0.50

    # Idempotency / dedup
    # Pending rows in ``order_attempts`` older than this threshold are
    # surfaced at cycle start as orphan warnings (operator reconciles
    # manually) and exported via ``orphan_order_attempts_total``. Twice
    # the cycle period gives the in-flight request all of its retries
    # plus one full follow-on cycle before raising the alarm.
    ORPHAN_ATTEMPT_WARN_AFTER_MIN: int = 30

    # Sanity gates
    ORDER_SANITY_MAX_PCT_EQUITY: float = 0.50
    ORDER_PRICE_MIN: float = 0.005
    ORDER_PRICE_MAX: float = 0.995
    ORDER_RATE_LIMIT_PER_HOUR: int = 100
    MAX_OPEN_POSITIONS: int = 50

    # Prometheus exporter (P1.1). Pull model: long-running daemon binds
    # METRICS_HOST:METRICS_PORT/metrics, Prometheus scrapes. Always 127.0.0.1
    # by default — exposing the dashboard externally would leak live strategy.
    METRICS_ENABLED: bool = True
    METRICS_HOST: str = "127.0.0.1"
    METRICS_PORT: int = 9100

    # Tier-1 Trade Evaluation Team (P2.1 / P2.3). One evaluator subagent per
    # role; the orchestrator caps each subagent's per-cycle Anthropic spend
    # so a runaway agent cannot burn the daily budget on a single tick.
    EVALUATION_LOOKBACK_DAYS: int = 30
    EVALUATION_TIMEOUT_SEC: int = 300
    BUDGET_USD_OUTCOME_FETCHER: float = 0.50
    BUDGET_USD_PNL_AGGREGATOR: float = 0.50
    BUDGET_USD_AGENT_PERFORMANCE_UPDATER: float = 0.50
