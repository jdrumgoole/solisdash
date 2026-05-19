"""Typed settings loaded from `solisdash.toml`, the project `.env`, and the
process environment.

Source precedence (later wins):
1. `~/.config/solisdash/solisdash.toml` — the in-browser setup wizard
   writes here. Cleanest for installed-from-PyPI users.
2. `./.env` in the current directory — dev-checkout convention; no longer
   used for installed users.
3. Process environment variables.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from solisdash.configfile import user_config_dir, user_config_toml_path

# Re-export so callers don't need both modules.
__all__ = ["Settings", "get_settings", "user_config_dir", "user_config_toml_path"]


class Settings(BaseSettings):
    """Process-wide configuration.

    `settings_customise_sources` re-resolves `user_config_toml_path()` on
    every `Settings()` instantiation so tests can redirect via
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
            TomlConfigSettingsSource(
                settings_cls, toml_file=str(user_config_toml_path())
            ),
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
    # Production opts in via `RUN_SCHEDULER=true` in the toml / env.
    RUN_SCHEDULER: bool = False
    SCHEDULER_SAMPLE_MINUTES: int = 5
    SCHEDULER_DAILY_HOUR_UTC: int = 0
    SCHEDULER_DAILY_MINUTE_UTC: int = 30
    SCHEDULER_RATE_PER_SEC: float = 1.5

    # Tariffs for the Cashflow chart — left at zero by default until the
    # user sets them. SolisCloud's `money` field is `production * tariff`
    # at whatever single rate they have configured upstream; it doesn't
    # capture real-world cashflow which depends on a different feed-in
    # rate vs a higher import rate.
    SOLIS_FEED_IN_TARIFF: float = 0.0     # EUR (or whatever currency) per exported kWh
    SOLIS_IMPORT_TARIFF: float = 0.0      # EUR per imported kWh
    SOLIS_CURRENCY: str = "EUR"


@lru_cache
def get_settings() -> Settings:
    return Settings()
