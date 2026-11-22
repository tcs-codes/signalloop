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
    # LLM replay cache (fixtures/llm): "auto" = replay in mock mode, off in live mode;
    # "replay" = serve recorded outputs, call live (and record) on a miss; "record" = always live,
    # save outputs; "off" = never touch the cache.
    LLM_REPLAY: str = "auto"
    LLM_MAX_TOKENS: int = 16000
    # Effort per model ("" = API default). Fast-model calls are extraction/checking/short notes:
    # live runs showed ~1.5k reasoning tokens and ~6s per call at the default effort.
    LLM_EFFORT_STRONG: str = ""
    LLM_EFFORT_FAST: str = "low"
    OUTREACH_PARALLELISM: int = 4
    # USD per 1M tokens (input, output) for the cost estimate on the Credits page
    LLM_PRICES: dict[str, tuple[float, float]] = {
        "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
        "claude-opus-5-5": (4.0, 20.0)}

    CREDIT_BUDGET_PER_RUN: float = 500
    DB_PATH: str = "data/signalloop.db"

    CRUSTDATA_BASE_URL: str = "https://api.crustdata.com"
    CRUSTDATA_API_VERSION: str = "2025-11-01"
    # Client-side spacing per endpoint in live mode. Starter tier allows 5 RPM on
    # search/enrich/identify; raise this if your tier is higher.
    CRUSTDATA_RPM: float = 5

    # FIND (F9, F11)
    LOOKALIKE_TARGET_MIN: int = 50
    LOOKALIKE_TARGET_MAX: int = 200
    LOOKALIKE_MAX_ITERS: int = 4
    LOOKALIKE_FETCH_LIMIT: int = 150   # results fetched once tuning has converged
    SIMILARITY_POLISH_TOP_N: int = 0   # >0: polish top-N similarity reasons with MODEL_FAST
    CONTACTS_TOP_N: int = 30
    CONTACTS_PER_ACCOUNT: int = 2

    # WATCH (F12)
    WATCH_FUNDING_DAYS: int = 90
    WATCH_GROWTH_MIN_PCT: float = 15.0
    WATCH_JOB_DAYS: int = 30
    WATCH_LEADER_DAYS: int = 90
    WATCH_DISCOVERY_LIMIT: int = 25    # new accounts per scan from the tuned ICP + recency (0 = off)
    WATCH_JOB_LIMIT: int = 100
    # Demo determinism: scans run "as of" this date when DEMO_MODE is on (YYYY-MM-DD, empty = today)
    DEMO_AS_OF: str = "2026-11-22"

    # Scoring weights (F14)
    PRIORITY_FIT_WEIGHT: float = 0.6
    PRIORITY_TIMING_WEIGHT: float = 0.4
    TIMING_DECAY_DAYS: float = 14.0    # default decay constant (job posts)
    # Per-type decay constants in days; 0 = no decay. Funding rounds are often announced weeks
    # after closing and stay a valid trigger for ~2 months; headcount growth is a state, not an event.
    TIMING_DECAY_BY_TYPE: dict[str, float] = {"funding": 45.0, "leader_change": 30.0,
                                              "job_post": 14.0, "headcount_growth": 0.0}
    GROWTH_MAX_STRENGTH: float = 0.5

    # ICP diff eligibility (F28)
    ICP_DIFF_MIN_N: int = 30
    ICP_DIFF_DEMO_MIN_N: int = 15
    DEMO_MODE: bool = True

    fixtures_dir: Path = Field(default=PROJECT_ROOT / "fixtures")
    exports_dir: Path = Field(default=PROJECT_ROOT / "exports")

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
