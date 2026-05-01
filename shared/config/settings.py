"""Single source of truth for all runtime tunables (per engineering.md §21).

All numerical thresholds, limits, parameters, and tunables that influence
runtime behavior live here. Services and agents import from this module —
no parallel constants, no scattered defaults, no magic numbers in business
logic. New tunables MUST be added here, never inlined in service code.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

TradingMode = Literal["paper", "real_capital"]


class Settings(BaseSettings):
    """Runtime configuration. Values come from environment / .env file.

    Per engineering.md §11: TRADING_MODE flips paper -> real_capital are
    deliberate code changes that go through PR review, never via env at runtime.
    The default is `paper` so a clean checkout cannot place real orders.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Mode ---
    trading_mode: TradingMode = "paper"

    # --- Database / cache ---
    database_url: str = "postgresql+psycopg://trading:trading_dev@localhost:5432/autonomous_trading"
    redis_url: str = "redis://localhost:6379/0"

    # --- LLM providers ---
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # --- Polymarket endpoints ---
    polymarket_clob_base: str = "https://clob.polymarket.com"
    polymarket_gamma_base: str = "https://gamma-api.polymarket.com"

    # --- Web search (PA-aligned, see research/skills/web_search.py) ---
    openai_web_search_model: str = "gpt-4o-mini"
    web_search_timeout_s: float = 120.0
    web_search_blacklist: list[str] = Field(default_factory=lambda: ["coinmarketcap.com"])

    # --- Logging ---
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide singleton Settings instance.

    Cached so repeated imports don't re-parse env. Tests can call
    `get_settings.cache_clear()` to refresh after env mutation.
    """
    return Settings()
