"""First-run setup wizard: locked behind an "any user exists" check."""

from __future__ import annotations

from typing import Any

import httpx
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.auth import create_user


async def test_setup_renders_when_no_users_yet(
    auth_client: httpx.AsyncClient,
) -> None:
    """The freshly-installed instance must offer a way to make the first admin."""
    r = await auth_client.get("/setup")
    assert r.status_code == 200
    body = r.text
    assert "Welcome to Solisdash" in body
    assert 'name="username"' in body
    assert 'name="password"' in body
    assert 'name="confirm"' in body


async def test_setup_redirects_to_login_once_any_user_exists(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """Once any account exists, the wizard turns into a redirect."""
    await create_user(clean_db, username="existing", password="x", role="admin")
    r = await auth_client.get("/setup", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_login_redirects_to_setup_when_no_users(
    auth_client: httpx.AsyncClient,
) -> None:
    r = await auth_client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


async def test_login_renders_form_once_a_user_exists(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await create_user(clean_db, username="joe", password="hunter2", role="admin")
    r = await auth_client.get("/login")
    assert r.status_code == 200
    assert "Sign in" in r.text


async def test_setup_creates_admin_and_redirects(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    r = await auth_client.post(
        "/setup",
        data={"username": "joe", "password": "hunter2", "confirm": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    user = await clean_db["users"].find_one({"username": "joe"})
    assert user is not None
    assert user["role"] == "admin"


async def test_setup_rejects_password_mismatch(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    r = await auth_client.post(
        "/setup",
        data={"username": "joe", "password": "hunter2", "confirm": "different"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Passwords do not match" in r.text
    # Username is sticky on re-render so the user doesn't retype it.
    assert 'value="joe"' in r.text
    assert await clean_db["users"].count_documents({}) == 0


async def test_setup_rejects_empty_password(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    r = await auth_client.post(
        "/setup",
        data={"username": "joe", "password": "", "confirm": ""},
        follow_redirects=False,
    )
    # Empty inputs are caught by the Form(...) requirement → 422 from FastAPI.
    # Our handler validates the same thing as a backup for transports that
    # somehow send empty values past the schema.
    assert r.status_code in (400, 422)
    assert await clean_db["users"].count_documents({}) == 0


async def test_setup_rejects_whitespace_only_username(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    r = await auth_client.post(
        "/setup",
        data={"username": "   ", "password": "hunter2", "confirm": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "Username must not be empty" in r.text
    assert await clean_db["users"].count_documents({}) == 0


async def test_setup_post_after_any_user_exists_redirects_without_creating(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """The "one time per install" lock: POST is also gated."""
    await create_user(clean_db, username="first", password="x", role="admin")
    r = await auth_client.post(
        "/setup",
        data={"username": "second", "password": "y", "confirm": "y"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    # Second user must NOT have been created.
    assert await clean_db["users"].count_documents({}) == 1
    assert await clean_db["users"].find_one({"username": "second"}) is None
