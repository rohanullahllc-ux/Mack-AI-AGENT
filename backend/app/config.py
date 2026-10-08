from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
SINGLE_USER_ID = "local"  # every table carries user_id so multi-tenant is a later config change


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    anthropic_api_key: str = ""

    agent_name: str = "Mack Agent"
    model_main: str = "claude-sonnet-5-5"
    effort: str = "medium"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    refusal_fallback: bool = True
    web_search_enabled: bool = True
    web_search_max_uses: int = 5

    # Cost / safety guardrails
    max_steps: int = 12
    max_run_cost_usd: float = 0.50
    monthly_budget_usd: float = 10.0
    tool_timeout_s: float = 60.0

    # Human-in-the-loop: tools at or above this tier wait for the user's approval before running.
    # "write" = ask for write + destructive tools, "destructive" = ask only for destructive, "never" = auto-run all.
    approval_level: Literal["write", "destructive", "never"] = "write"
    approval_timeout_s: float = 300.0  # no answer in time = denied (a stuck run would block its conversation)

    app_token: str = ""
    demo_mode: bool = False
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3100",
        "http://127.0.0.1:3100",
    ]

    data_dir: Path = BACKEND_DIR / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / ("demo.db" if self.demo_mode else "agent.db")

    @property
    def sandbox_dir(self) -> Path:
        return self.data_dir / ("demo_sandbox" if self.demo_mode else "sandbox")


@lru_cache
def get_settings() -> Settings:
    return Settings()
