"""Single source of truth for all numeric tunables and runtime settings.

Per CLAUDE.md and engineering.md §10, every threshold, limit, and parameter
lives here. Never hardcode values in risk/, execution/, or research/.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

TradingMode = Literal["paper", "real_capital"]
KeyProviderKind = Literal["encrypted_file", "aws_kms"]
SystemStateKey = Literal[
    "kill_switch",
    "last_cycle_ts",
    "last_outcome_ingestion_at",
    "last_lessons_summary_at",
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
    EDGE_THRESHOLD: float = 0.03
    CYCLE_PERIOD_MIN: int = 12
    TOP_K_MARKETS: int = 50

    # Risk caps (decimals, e.g. 0.15 = 15%)
    CONCENTRATION_CAP: float = 0.15
    CYCLE_CAP: float = 0.25

    # Lessons / learning
    LESSONS_TOP_K: int = 10
    LESSONS_LOOKBACK_DAYS: int = 30
    SURPRISE_THRESHOLD: float = 0.3

    # Timeouts
    WEB_SEARCH_TIMEOUT_SEC: int = 60
    AGENT_TIMEOUT_SEC: int = 300

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
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4.1"

    # Reconciliation
    RECONCILIATION_DIFF_USD: float = 0.50

    # Sanity gates
    ORDER_SANITY_MAX_PCT_EQUITY: float = 0.50
    ORDER_PRICE_MIN: float = 0.005
    ORDER_PRICE_MAX: float = 0.995
    ORDER_RATE_LIMIT_PER_HOUR: int = 100
    MAX_OPEN_POSITIONS: int = 50
