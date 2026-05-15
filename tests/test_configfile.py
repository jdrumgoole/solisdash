from __future__ import annotations

from pathlib import Path

import pytest

from solisdash.configfile import (
    PERSISTED_KEYS,
    delete_toml,
    read_toml,
    user_config_dir,
    user_config_toml_path,
    write_toml,
)


def test_user_config_dir_honours_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert user_config_dir() == tmp_path / "solisdash"
    assert user_config_toml_path() == tmp_path / "solisdash" / "solisdash.toml"


def test_user_config_dir_falls_back_to_home_dot_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert user_config_dir() == Path.home() / ".config" / "solisdash"


def test_read_toml_returns_empty_when_missing(tmp_path: Path) -> None:
    assert read_toml(tmp_path / "nope.toml") == {}


def test_read_toml_tolerates_parse_errors(tmp_path: Path) -> None:
    f = tmp_path / "bad.toml"
    f.write_text("this = is not valid toml ===")
    assert read_toml(f) == {}


def test_write_toml_round_trips_flat_keys(tmp_path: Path) -> None:
    f = tmp_path / "solisdash.toml"
    write_toml(
        {
            "SOLIS_MONGODB_URI": "mongodb://localhost:27017/",
            "SOLIS_KEY_ID": "abc",
            "SESSION_SECRET": "deadbeef",
        },
        path=f,
    )
    assert read_toml(f) == {
        "SOLIS_MONGODB_URI": "mongodb://localhost:27017/",
        "SOLIS_KEY_ID": "abc",
        "SESSION_SECRET": "deadbeef",
    }


def test_write_toml_merges_with_existing(tmp_path: Path) -> None:
    f = tmp_path / "solisdash.toml"
    write_toml({"SOLIS_KEY_ID": "old", "SESSION_SECRET": "keep"}, path=f)
    write_toml({"SOLIS_KEY_ID": "new", "SOLIS_API_URL": "https://api/"}, path=f)
    assert read_toml(f) == {
        "SOLIS_KEY_ID": "new",
        "SESSION_SECRET": "keep",
        "SOLIS_API_URL": "https://api/",
    }


def test_write_toml_skips_empty_values(tmp_path: Path) -> None:
    f = tmp_path / "solisdash.toml"
    write_toml({"SOLIS_KEY_ID": "abc", "SOLIS_KEYSECRET": "", "SESSION_SECRET": None}, path=f)
    saved = read_toml(f)
    assert saved == {"SOLIS_KEY_ID": "abc"}


def test_write_toml_chmods_owner_only(tmp_path: Path) -> None:
    f = tmp_path / "solisdash.toml"
    write_toml({"SOLIS_KEY_ID": "secret-ish"}, path=f)
    mode = f.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_toml_orders_known_keys_first(tmp_path: Path) -> None:
    f = tmp_path / "solisdash.toml"
    # Write in random order; PERSISTED_KEYS order should win on disk.
    write_toml(
        {
            "SESSION_SECRET": "z",
            "SOLIS_API_URL": "https://api/",
            "SOLIS_MONGODB_URI": "mongodb://a/",
        },
        path=f,
    )
    body = f.read_text()
    # Mongo URI appears before API URL appears before SESSION_SECRET in PERSISTED_KEYS.
    assert body.index("SOLIS_MONGODB_URI") < body.index("SOLIS_API_URL")
    assert body.index("SOLIS_API_URL") < body.index("SESSION_SECRET")


def test_delete_toml_returns_true_when_existed(tmp_path: Path) -> None:
    f = tmp_path / "solisdash.toml"
    f.write_text("FOO = 'bar'")
    assert delete_toml(path=f) is True
    assert not f.exists()


def test_delete_toml_returns_false_when_missing(tmp_path: Path) -> None:
    assert delete_toml(path=tmp_path / "nope.toml") is False


def test_persisted_keys_includes_core_settings() -> None:
    """Lock in the keys the wizard / settings page is expected to write."""
    expected = {
        "SOLIS_MONGODB_URI",
        "SOLIS_MONGODB_DB",
        "SOLIS_API_URL",
        "SOLIS_KEY_ID",
        "SOLIS_KEYSECRET",
        "SOLIS_STATION_ID",
        "SESSION_SECRET",
    }
    assert expected.issubset(set(PERSISTED_KEYS))
