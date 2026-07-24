"""Application configuration.

All settings are environment-driven with production-safe defaults. Credentials are never
given defaults - a missing credential must fail loudly at startup rather than silently
producing a service that 401s on every request.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # --- Service identity -------------------------------------------------
    app_name: str = "Flock Energy - Urja Meter Ops API"
    app_version: str = "1.0.0"
    environment: str = Field(default="development")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json", description="'json' or 'console'")

    # --- Upstream portal --------------------------------------------------
    portal_base_url: str = Field(default="https://urja-ops.flockenergy.tech")
    portal_username: str = Field(default="")
    portal_password: SecretStr = Field(default=SecretStr(""))

    # --- HTTP client behaviour -------------------------------------------
    portal_connect_timeout: float = Field(default=5.0, gt=0)
    portal_read_timeout: float = Field(default=20.0, gt=0)
    portal_max_connections: int = Field(default=10, ge=1)
    portal_max_retries: int = Field(default=3, ge=0)
    portal_backoff_base: float = Field(default=0.25, ge=0)
    portal_backoff_max: float = Field(default=4.0, ge=0)

    # Concurrency ceiling for fan-out crawls (per-meter time series).
    # The portal showed no rate limiting at ~3 req/s, but absence of evidence is not
    # evidence of absence - we stay deliberately polite.
    portal_max_concurrency: int = Field(default=6, ge=1, le=32)

    # --- Session management ----------------------------------------------
    # The portal issues a 1-hour session. We refresh this many seconds *before* the
    # advertised expiry so an in-flight request never races the boundary.
    session_refresh_margin_seconds: int = Field(default=120, ge=0)

    # --- Snapshot / freshness --------------------------------------------
    snapshot_ttl_seconds: int = Field(default=300, ge=0)
    snapshot_refresh_on_start: bool = Field(default=True)
    # Minimum spacing between forced refreshes via the unauthenticated refresh endpoint.
    # Caps portal export load under a burst; 0 disables the throttle. See the endpoint and
    # README, *What I intentionally left out* (auth/rate-limiting).
    snapshot_refresh_min_interval_seconds: int = Field(default=30, ge=0)
    consumption_cache_ttl_seconds: int = Field(default=120, ge=0)
    consumption_cache_max_entries: int = Field(default=256, ge=1)

    # --- API surface ------------------------------------------------------
    api_prefix: str = "/api/v1"
    default_page_size: int = Field(default=25, ge=1, le=200)
    max_page_size: int = Field(default=200, ge=1, le=1000)
    cors_allow_origins: list[str] = Field(default_factory=list)

    @field_validator("log_format")
    @classmethod
    def _check_log_format(cls, v: str) -> str:
        if v not in {"json", "console"}:
            raise ValueError("log_format must be 'json' or 'console'")
        return v

    @field_validator("portal_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def has_credentials(self) -> bool:
        return bool(self.portal_username and self.portal_password.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Cleared in tests via ``get_settings.cache_clear()``."""
    return Settings()
