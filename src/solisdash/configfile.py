"""TOML config file at ``~/.config/solisdash/solisdash.toml``.

The browser setup wizard writes here. `Settings` reads it (via
pydantic-settings' `TomlConfigSettingsSource`) with environment variables
overriding anything in the file.

Flat top-level keys only — no `[section]` nesting — so the toml schema
mirrors the env-var names 1-to-1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import tomli_w

# Stdlib in 3.11+, optional dep otherwise. We require pydantic-settings>=2.6,
# which already pulls in `tomli` on <3.11, so this import works everywhere.
try:
    import tomllib  # type: ignore[import-not-found, unused-ignore]
except ImportError:  # pragma: no cover — exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef, import-not-found, unused-ignore]

# Keys the setup wizard persists. Order chosen for readability when a user
# hand-edits the file later.
PERSISTED_KEYS: tuple[str, ...] = (
    "SOLIS_MONGODB_URI",
    "SOLIS_MONGODB_DB",
    "SOLIS_API_URL",
    "SOLIS_KEY_ID",
    "SOLIS_KEYSECRET",
    "SOLIS_STATION_ID",
    "SESSION_SECRET",
    "RUN_SCHEDULER",
    "SCHEDULER_SAMPLE_MINUTES",
    "SCHEDULER_DAILY_HOUR_UTC",
    "SCHEDULER_DAILY_MINUTE_UTC",
    "SCHEDULER_RATE_PER_SEC",
)


def user_config_dir() -> Path:
    """Per-user config directory (honours `XDG_CONFIG_HOME`)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "solisdash" if base else Path.home() / ".config" / "solisdash"


def user_config_toml_path() -> Path:
    """`solisdash.toml` location. Re-resolved every call so tests can redirect."""
    return user_config_dir() / "solisdash.toml"


def read_toml(path: Path | None = None) -> dict[str, Any]:
    """Return the current contents of the toml file, or `{}` if it doesn't exist.

    Falls back to an empty dict on any parse error — the wizard will simply
    treat the install as un-configured and offer to write a fresh file.
    """
    target = path or user_config_toml_path()
    if not target.exists():
        return {}
    try:
        parsed: dict[str, Any] = tomllib.loads(target.read_text())
        return parsed
    except tomllib.TOMLDecodeError:
        return {}


def write_toml(values: dict[str, Any], *, path: Path | None = None) -> Path:
    """Merge `values` into the toml file and write atomically.

    Empty / None values are skipped (we don't want to persist
    ``SOLIS_KEY_ID = ""``). The file is chmodded to 0600 so a shared host
    doesn't leak credentials to other users.
    """
    target = path or user_config_toml_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    merged = read_toml(target)
    for k, v in values.items():
        if v is None or v == "":
            continue
        merged[k] = v
    # Re-order keys to match PERSISTED_KEYS where possible, then append unknowns.
    ordered: dict[str, Any] = {k: merged[k] for k in PERSISTED_KEYS if k in merged}
    for k, v in merged.items():
        if k not in ordered:
            ordered[k] = v
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(tomli_w.dumps(ordered).encode("utf-8"))
    tmp.replace(target)
    try:
        target.chmod(0o600)
    except OSError:
        pass  # best-effort on filesystems that don't support it
    return target


def delete_toml(*, path: Path | None = None) -> bool:
    """Remove the toml file. Returns True if it existed."""
    target = path or user_config_toml_path()
    if target.exists():
        target.unlink()
        return True
    return False
