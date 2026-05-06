"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All env-tunable knobs live here."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="TON_PNL_", extra="ignore")

    tonapi_base_url: str = "https://tonapi.io"
    tonapi_token: str | None = None

    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    geckoterminal_network: str = "ton"

    cache_ttl_seconds: int = 300
    max_events_per_wallet: int = 1000
    request_timeout_seconds: float = 30.0

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:4173",
        "http://127.0.0.1:5173",
    ]


settings = Settings()
