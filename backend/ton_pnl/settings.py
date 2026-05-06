"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import json
import logging

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


def _parse_kv_json(value: str | dict[str, str] | None) -> dict[str, str]:
    """Parse env-var inputs that may be JSON objects or already-decoded dicts.

    pydantic-settings will hand us either a raw string from ``os.environ`` or,
    when reading the ``.env`` file, a string that still needs JSON decoding.
    Empty / missing values normalize to an empty dict.
    """

    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        log.warning("failed to parse JSON header/cookie config; ignoring")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


class Settings(BaseSettings):
    """All env-tunable knobs live here."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="TON_PNL_", extra="ignore")

    # Defaults to the public tonapi.io. Set this to e.g.
    # ``https://tonviewer.com/api/tonapi`` to route through the tonviewer
    # frontend proxy when their normal limits are insufficient.
    tonapi_base_url: str = "https://tonapi.io"
    tonapi_token: str | None = None

    # Extra HTTP headers to include with every tonapi call. Useful when tunneling
    # through a frontend proxy (tonviewer.com) that requires browser-like
    # ``User-Agent`` / ``Referer`` / ``sec-ch-ua-*`` headers.
    tonapi_headers: dict[str, str] = Field(default_factory=dict)
    # Cookies forwarded with every tonapi call (same use case as ``tonapi_headers``).
    tonapi_cookies: dict[str, str] = Field(default_factory=dict)

    # When using the tonviewer.com frontend proxy, responses come back as
    # CryptoJS-style AES (``Salted__...`` base64). The default passphrase is the
    # one their app uses today; override if they ever rotate it.
    tonviewer_passphrase: str = "tv22-asrr11"

    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    geckoterminal_network: str = "ton"

    cache_ttl_seconds: int = 300
    max_events_per_wallet: int = 1000
    tonapi_rps: float = 2.0
    tonapi_event_batch_size: int = 30
    request_timeout_seconds: float = 30.0

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:4173",
        "http://127.0.0.1:5173",
    ]

    @field_validator("tonapi_headers", "tonapi_cookies", mode="before")
    @classmethod
    def _decode_kv(cls, value: str | dict[str, str] | None) -> dict[str, str]:
        return _parse_kv_json(value)


settings = Settings()
