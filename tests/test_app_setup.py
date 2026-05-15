"""First-run setup wizard + /settings page.

The wizard is rendered when `_setup_done()` returns False — i.e. either no
Mongo URI is configured or no user has been created yet. Tests monkey-patch
`_setup_done` / `_users_exist_in` where they want explicit control, so they
don't depend on whatever the env contributes via `settings_customise_sources`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash import app as app_module
from solisdash.auth import create_user

# --- /setup GET ------------------------------------------------------------


async def test_setup_renders_three_section_wizard(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh install: wizard shows MongoDB, SolisCloud and Admin sections."""

    async def _not_done() -> bool:
        return False

    monkeypatch.setattr(app_module, "_setup_done", _not_done)

    r = await auth_client.get("/setup")
    assert r.status_code == 200
    body = r.text
    assert "Welcome to Solisdash" in body
    assert "1. MongoDB" in body
    assert "2. SolisCloud" in body
    assert "3. Administrator account" in body
    # HTMX wiring for inline test buttons.
    assert 'hx-post="/setup/test/mongo"' in body
    assert 'hx-post="/setup/test/soliscloud"' in body
    # Fields the POST handler expects.
    for name in (
        "mongo_uri",
        "mongo_db",
        "solis_api_url",
        "solis_key_id",
        "solis_keysecret",
        "username",
        "password",
        "confirm",
    ):
        assert f'name="{name}"' in body


async def test_setup_redirects_to_login_once_setup_is_done(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once Mongo is wired AND a user exists, /setup hands off to /login."""

    async def _done() -> bool:
        return True

    monkeypatch.setattr(app_module, "_setup_done", _done)

    r = await auth_client.get("/setup", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_login_redirects_to_setup_when_setup_incomplete(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _not_done() -> bool:
        return False

    monkeypatch.setattr(app_module, "_setup_done", _not_done)

    r = await auth_client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


async def test_login_renders_form_once_setup_done(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _done() -> bool:
        return True

    monkeypatch.setattr(app_module, "_setup_done", _done)

    r = await auth_client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


# --- /setup HTMX test endpoints -------------------------------------------


async def test_setup_test_mongo_reports_failure_for_unreachable_uri(
    auth_client: httpx.AsyncClient,
) -> None:
    """Port 1 should never run mongod on the CI box."""
    r = await auth_client.post(
        "/setup/test/mongo",
        data={"mongo_uri": "mongodb://127.0.0.1:1/", "mongo_db": "solis"},
    )
    assert r.status_code == 200  # HTMX fragments always return 200
    assert "test-result error" in r.text
    assert "MongoDB connection failed" in r.text


async def test_setup_test_soliscloud_demands_both_id_and_secret(
    auth_client: httpx.AsyncClient,
) -> None:
    r = await auth_client.post(
        "/setup/test/soliscloud",
        data={
            "solis_api_url": "https://www.soliscloud.com:13333",
            "solis_key_id": "",
            "solis_keysecret": "",
        },
    )
    assert r.status_code == 200
    assert "Enter both Key ID and Key Secret" in r.text


async def test_setup_test_soliscloud_renders_error_on_transport_failure(
    auth_client: httpx.AsyncClient,
) -> None:
    """Pointing at a non-routable host produces a connection error."""
    r = await auth_client.post(
        "/setup/test/soliscloud",
        data={
            # 0.0.0.0:1 is non-routable — fast-failing connect with no DNS.
            "solis_api_url": "http://0.0.0.0:1",
            "solis_key_id": "k",
            "solis_keysecret": "s",
        },
    )
    assert r.status_code == 200
    assert "test-result error" in r.text


# --- /setup POST ----------------------------------------------------------


async def test_setup_post_redirects_when_setup_already_done(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _done() -> bool:
        return True

    monkeypatch.setattr(app_module, "_setup_done", _done)

    r = await auth_client.post(
        "/setup",
        data={
            "mongo_uri": "mongodb://x/",
            "mongo_db": "solis",
            "solis_api_url": "https://api/",
            "solis_key_id": "",
            "solis_keysecret": "",
            "username": "joe",
            "password": "hunter2",
            "confirm": "hunter2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_setup_post_rejects_password_mismatch(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _not_done() -> bool:
        return False

    monkeypatch.setattr(app_module, "_setup_done", _not_done)

    r = await auth_client.post(
        "/setup",
        data={
            "mongo_uri": "mongodb://127.0.0.1:1/",
            "mongo_db": "solis",
            "solis_api_url": "https://api/",
            "solis_key_id": "",
            "solis_keysecret": "",
            "username": "joe",
            "password": "a",
            "confirm": "b",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Passwords do not match" in r.text
    assert 'value="joe"' in r.text  # username sticky on re-render


async def test_setup_post_rejects_whitespace_only_username(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _not_done() -> bool:
        return False

    monkeypatch.setattr(app_module, "_setup_done", _not_done)

    r = await auth_client.post(
        "/setup",
        data={
            "mongo_uri": "mongodb://127.0.0.1:1/",
            "mongo_db": "solis",
            "solis_api_url": "https://api/",
            "solis_key_id": "",
            "solis_keysecret": "",
            "username": "   ",
            "password": "hunter2",
            "confirm": "hunter2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Username must not be empty" in r.text


async def test_setup_post_rejects_unreachable_mongo(
    auth_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connection probe must reject before we write any toml."""

    async def _not_done() -> bool:
        return False

    monkeypatch.setattr(app_module, "_setup_done", _not_done)

    r = await auth_client.post(
        "/setup",
        data={
            "mongo_uri": "mongodb://127.0.0.1:1/",
            "mongo_db": "solis",
            "solis_api_url": "https://api/",
            "solis_key_id": "",
            "solis_keysecret": "",
            "username": "joe",
            "password": "hunter2",
            "confirm": "hunter2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "MongoDB connection failed" in r.text


async def test_setup_post_rejects_existing_username_in_target_db(
    auth_client: httpx.AsyncClient,
    clean_db: AsyncDatabase[dict[str, Any]],
    mongo_uri: str,
    test_db_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Pointing the wizard at a DB that already has the chosen username
    must render a friendly error, not blow up with DuplicateKeyError."""

    async def _not_done() -> bool:
        return False

    monkeypatch.setattr(app_module, "_setup_done", _not_done)
    # Keep any toml writes off the user's real home directory.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # Seed the target DB with a user the form will collide with.
    await create_user(clean_db, username="jdrumgoole", password="x", role="admin")

    r = await auth_client.post(
        "/setup",
        data={
            "mongo_uri": mongo_uri,
            "mongo_db": test_db_name,
            "solis_api_url": "https://api/",
            "solis_key_id": "",
            "solis_keysecret": "",
            "username": "jdrumgoole",
            "password": "hunter2",
            "confirm": "hunter2",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "already exists" in r.text
    assert "jdrumgoole" in r.text
    # Pre-check fires before write_toml, so the toml was not written.
    assert not (tmp_path / "solisdash" / "solisdash.toml").exists()
    # And the seeded user remains untouched.
    assert await clean_db["users"].count_documents({"username": "jdrumgoole"}) == 1


# --- /settings page -------------------------------------------------------


async def test_settings_requires_auth(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.get("/settings", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.fixture
async def signed_in_client(
    auth_client: httpx.AsyncClient,
    clean_db: AsyncDatabase[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """An `auth_client` with a fresh admin account already signed in."""

    async def _done() -> bool:
        return True

    monkeypatch.setattr(app_module, "_setup_done", _done)
    await create_user(clean_db, username="joe", password="hunter2", role="admin")
    r = await auth_client.post(
        "/login",
        data={"username": "joe", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    yield auth_client


async def test_settings_renders_when_signed_in(
    signed_in_client: httpx.AsyncClient,
) -> None:
    r = await signed_in_client.get("/settings")
    assert r.status_code == 200
    body = r.text
    assert "/settings/save" in body
    assert "/settings/reset" in body
    assert 'name="mongo_uri"' in body
    assert 'name="solis_keysecret"' in body


async def test_settings_reset_logs_out_and_redirects_to_setup(
    signed_in_client: httpx.AsyncClient,
) -> None:
    r = await signed_in_client.post("/settings/reset", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"
