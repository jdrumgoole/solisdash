"""First-run interactive onboarding helpers.

These tests stub `input` / `getpass` so the assertions are deterministic.
No real Mongo / SolisCloud / pywebview calls happen here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from solisdash.cli import (
    _apply_env,
    _prompt,
    _read_kv,
    _write_kv,
    interactive_setup,
)
from solisdash.config import get_settings, user_config_dir, user_config_path

# --- _prompt ---------------------------------------------------------------


def test_prompt_returns_input_when_supplied() -> None:
    got = _prompt(
        "MongoDB", default="mongodb://localhost:27017/",
        input_fn=lambda _msg: "mongodb://other:27018/",
    )
    assert got == "mongodb://other:27018/"


def test_prompt_falls_back_to_default_when_input_empty() -> None:
    got = _prompt(
        "MongoDB", default="mongodb://localhost:27017/",
        input_fn=lambda _msg: "",
    )
    assert got == "mongodb://localhost:27017/"


def test_prompt_uses_getpass_in_secret_mode() -> None:
    calls: list[str] = []

    def fake_getpass(prompt: str) -> str:
        calls.append(prompt)
        return "shhh"

    got = _prompt(
        "SolisCloud Key Secret",
        secret=True,
        input_fn=lambda _msg: pytest.fail("input should not run in secret mode"),
        getpass_fn=fake_getpass,
    )
    assert got == "shhh"
    assert len(calls) == 1
    # Default isn't echoed in the prompt when secret=True (avoid leaking it).
    assert "[" not in calls[0]


# --- _read_kv / _write_kv --------------------------------------------------


def test_read_kv_parses_simple_env_file(tmp_path: Path) -> None:
    f = tmp_path / "config"
    f.write_text(
        "# comment\n"
        "\n"
        "FOO=bar\n"
        "  BAZ = qux  \n"
        "MALFORMED_LINE_NO_EQUALS\n"
    )
    assert _read_kv(f) == {"FOO": "bar", "BAZ": "qux"}


def test_read_kv_returns_empty_for_missing_file(tmp_path: Path) -> None:
    assert _read_kv(tmp_path / "nope") == {}


def test_write_kv_creates_file_with_keys(tmp_path: Path) -> None:
    f = tmp_path / "deep" / "config"
    _write_kv(f, {"FOO": "bar", "BAZ": "qux"})
    assert f.exists()
    assert _read_kv(f) == {"FOO": "bar", "BAZ": "qux"}


def test_write_kv_merges_with_existing(tmp_path: Path) -> None:
    f = tmp_path / "config"
    _write_kv(f, {"KEEP": "1", "REPLACE": "old"})
    _write_kv(f, {"REPLACE": "new", "ADDED": "yes"})
    assert _read_kv(f) == {"KEEP": "1", "REPLACE": "new", "ADDED": "yes"}


def test_write_kv_skips_empty_values(tmp_path: Path) -> None:
    f = tmp_path / "config"
    _write_kv(f, {"FOO": "bar", "EMPTY": ""})
    assert _read_kv(f) == {"FOO": "bar"}


def test_write_kv_sets_owner_only_permissions(tmp_path: Path) -> None:
    f = tmp_path / "config"
    _write_kv(f, {"SECRET": "x"})
    mode = f.stat().st_mode & 0o777
    assert mode == 0o600


# --- _apply_env ------------------------------------------------------------


def test_apply_env_updates_os_environ_and_clears_settings_cache(
    _isolated_config: Path,
) -> None:
    """The _isolated_config fixture parks us in an empty cwd with no `.env`."""
    assert get_settings().SOLIS_MONGODB_URI == ""

    _apply_env({"SOLIS_MONGODB_URI": "mongodb://x", "SESSION_SECRET": "abc"})
    assert os.environ["SOLIS_MONGODB_URI"] == "mongodb://x"
    assert get_settings().SOLIS_MONGODB_URI == "mongodb://x"
    assert get_settings().SESSION_SECRET == "abc"


# --- interactive_setup ----------------------------------------------------


@pytest.fixture
def _isolated_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Isolated config: tmp XDG_CONFIG_HOME, tmp cwd (no project .env), env cleared."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for k in (
        "SOLIS_MONGODB_URI",
        "SOLIS_KEY_ID",
        "SOLIS_KEYSECRET",
        "SOLIS_API_URL",
        "SESSION_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    yield user_config_path()
    get_settings.cache_clear()


def test_interactive_setup_skips_when_mongo_already_set(
    _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLIS_MONGODB_URI", "mongodb://existing/")
    monkeypatch.setenv("SESSION_SECRET", "already-set")
    get_settings.cache_clear()
    result = interactive_setup(
        is_tty_fn=lambda: pytest.fail("should not check tty when already set"),
        input_fn=lambda _msg: pytest.fail("should not prompt when already set"),
    )
    assert result == "skip"


def test_interactive_setup_aborts_when_unset_and_no_tty(
    _isolated_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    messages: list[str] = []
    result = interactive_setup(
        is_tty_fn=lambda: False,
        input_fn=lambda _msg: pytest.fail("no input expected"),
        out=messages.append,
    )
    assert result == "aborted"
    assert any("isn't a terminal" in m for m in messages)


def test_interactive_setup_writes_config_with_defaults(
    _isolated_config: Path,
) -> None:
    """Empty input → defaults used; key fields filled in."""
    answers = iter([
        "",   # Mongo URI → default
        "",   # API URL → default
        "",   # Key ID → blank, skip secret
        # No secret prompt because key id is blank
    ])

    result = interactive_setup(
        is_tty_fn=lambda: True,
        input_fn=lambda _msg: next(answers),
        getpass_fn=lambda _msg: pytest.fail("getpass should not run when key id is empty"),
        out=lambda _msg: None,
    )
    assert result == "configured"

    saved = _read_kv(_isolated_config)
    assert saved["SOLIS_MONGODB_URI"] == "mongodb://localhost:27017/"
    assert saved["SOLIS_API_URL"] == "https://www.soliscloud.com:13333"
    assert "SOLIS_KEY_ID" not in saved  # empty values aren't persisted
    assert "SOLIS_KEYSECRET" not in saved
    assert saved["SESSION_SECRET"]  # auto-generated
    assert len(saved["SESSION_SECRET"]) >= 32


def test_interactive_setup_prompts_for_secret_when_key_id_given(
    _isolated_config: Path,
) -> None:
    answers = iter([
        "mongodb://atlas/",
        "https://api.example/",
        "MY-KEY-ID",
    ])
    secrets_iter = iter(["MY-KEY-SECRET"])

    result = interactive_setup(
        is_tty_fn=lambda: True,
        input_fn=lambda _msg: next(answers),
        getpass_fn=lambda _msg: next(secrets_iter),
        out=lambda _msg: None,
    )
    assert result == "configured"
    saved = _read_kv(_isolated_config)
    assert saved["SOLIS_KEY_ID"] == "MY-KEY-ID"
    assert saved["SOLIS_KEYSECRET"] == "MY-KEY-SECRET"
    assert saved["SOLIS_MONGODB_URI"] == "mongodb://atlas/"


def test_interactive_setup_silently_fills_in_session_secret_when_mongo_set(
    _isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mongo already configured but SESSION_SECRET missing → no prompt, just generate."""
    monkeypatch.setenv("SOLIS_MONGODB_URI", "mongodb://existing/")
    get_settings.cache_clear()
    result = interactive_setup(
        is_tty_fn=lambda: True,
        input_fn=lambda _msg: pytest.fail("no prompts when mongo is set"),
        out=lambda _msg: None,
    )
    assert result == "skip"
    saved = _read_kv(_isolated_config)
    assert saved.get("SESSION_SECRET")
    assert len(saved["SESSION_SECRET"]) >= 32


def test_user_config_dir_honours_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert user_config_dir() == tmp_path / "solisdash"


def test_user_config_dir_falls_back_to_home_dot_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert user_config_dir() == Path.home() / ".config" / "solisdash"
