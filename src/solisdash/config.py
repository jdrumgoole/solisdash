"""Typed settings loaded from environment / .env files."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def user_config_dir() -> Path:
    """Per-user config directory.

    Honours `XDG_CONFIG_HOME` on every platform — macOS often doesn't, but
    one consistent path beats the platform-specific tangle.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "solisdash" if base else Path.home() / ".config" / "solisdash"


def user_config_path() -> Path:
    """`.env` the `solisdash` CLI writes first-run configuration into."""
    return user_config_dir() / ".env"


class Settings(BaseSettings):
    """Process-wide configuration.

    Source precedence (later wins):
    1. `~/.config/solisdash/.env` — the CLI writes first-run config here,
       so installs from PyPI have a home for it.
    2. `./.env` in the current directory — dev-checkout convention.
    3. Process environment variables.

    `settings_customise_sources` re-resolves `user_config_path()` on every
    `Settings()` instantiation so tests can redirect via
    `XDG_CONFIG_HOME` / `HOME` without restarting the process.
    """

    model_config = SettingsConfigDict(extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # pydantic-settings v2: earlier sources in this tuple win.
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(settings_cls, env_file=".env"),
            DotEnvSettingsSource(settings_cls, env_file=str(user_config_path())),
            file_secret_settings,
        )

    # SolisCloud
    SOLIS_KEY_ID: str = ""
    SOLIS_KEYSECRET: str = ""
    SOLIS_API_URL: str = "https://www.soliscloud.com:13333"
    SOLIS_STATION_ID: str = ""  # optional pinning; empty = pick first station

    # MongoDB
    SOLIS_MONGODB_URI: str = ""
    SOLIS_MONGODB_DB: str = "solis"

    # Web
    SESSION_SECRET: str = Field(default="", min_length=0)

    # Scheduler — off by default so tests and CI never poll SolisCloud.
    # Production opts in via `RUN_SCHEDULER=true` in `.env`.
    RUN_SCHEDULER: bool = False
    SCHEDULER_SAMPLE_MINUTES: int = 5
    SCHEDULER_DAILY_HOUR_UTC: int = 0
    SCHEDULER_DAILY_MINUTE_UTC: int = 30
    SCHEDULER_RATE_PER_SEC: float = 1.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
