"""Typed settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration. Read from environment or `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
