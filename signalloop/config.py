"""Runtime configuration loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    ANTHROPIC_API_KEY: str = ""
    CRUSTDATA_API_KEY: str = ""
    SIGNALLOOP_MODE: Literal["mock", "live"] = "mock"

    MODEL_STRONG: str = "claude-sonnet-5-5"
    MODEL_FAST: str = "claude-haiku-5-5"
    # Server-side refusal fallback (Anthropic beta); only applied to models that support it.
    LLM_REFUSAL_FALLBACK: bool = True
    LLM_MAX_TOKENS: int = 16000

    CREDIT_BUDGET_PER_RUN: float = 500
    DB_PATH: str = "data/signalloop.db"

    CRUSTDATA_BASE_URL: str = "https://api.crustdata.com"
    CRUSTDATA_API_VERSION: str = "2025-11-01"
    # Client-side spacing per endpoint in live mode. Starter tier allows 5 RPM on
    # search/enrich/identify; raise this if your tier is higher.
    CRUSTDATA_RPM: float = 5

    # Scoring weights (F14)
    PRIORITY_FIT_WEIGHT: float = 0.6
    PRIORITY_TIMING_WEIGHT: float = 0.4
    TIMING_DECAY_DAYS: float = 14.0

    # ICP diff eligibility (F28)
    ICP_DIFF_MIN_N: int = 30
    ICP_DIFF_DEMO_MIN_N: int = 15
    DEMO_MODE: bool = True

    fixtures_dir: Path = Field(default=PROJECT_ROOT / "fixtures")

    @property
    def db_path(self) -> Path:
        p = Path(self.DB_PATH)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def is_mock(self) -> bool:
        return self.SIGNALLOOP_MODE == "mock"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Clear the cached settings (used by tests after changing env vars)."""
    get_settings.cache_clear()
