from __future__ import annotations

from typing import Any

import httpx
from fastapi.testclient import TestClient
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.auth import create_user

# --- public surface --------------------------------------------------------


def test_health_remains_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_get_login_renders_form(client: TestClient) -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text.lower()
    assert 'name="username"' in r.text
    assert 'name="password"' in r.text


def test_unauthed_home_redirects_to_login(client: TestClient) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_unauthed_home_returns_401_for_api_clients(client: TestClient) -> None:
    r = client.get("/", headers={"accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401


def test_unauthed_home_returns_401_for_htmx(client: TestClient) -> None:
    r = client.get("/", headers={"hx-request": "true"}, follow_redirects=False)
    assert r.status_code == 401


# --- login / logout flow ---------------------------------------------------


async def _seed_user(
    db: AsyncDatabase[dict[str, Any]], *, username: str, password: str
) -> None:
    await create_user(db, username=username, password=password, role="admin")


async def test_login_with_valid_credentials_sets_session_and_redirects(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_user(clean_db, username="joe", password="hunter2")
    r = await auth_client.post(
        "/login",
        data={"username": "joe", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "session" in r.cookies


async def test_login_with_bad_password_renders_form_with_error(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_user(clean_db, username="joe", password="hunter2")
    r = await auth_client.post(
        "/login",
        data={"username": "joe", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Invalid username or password" in r.text
    # Sticky username in the re-rendered form
    assert 'value="joe"' in r.text


async def test_login_with_unknown_user_renders_error_not_404(
    auth_client: httpx.AsyncClient,
) -> None:
    r = await auth_client.post(
        "/login",
        data={"username": "ghost", "password": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert "Invalid username or password" in r.text


async def test_full_login_logout_cycle(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_user(clean_db, username="joe", password="hunter2")

    # Unauthed: home → /login
    r = await auth_client.get("/", follow_redirects=False)
    assert r.status_code == 303

    # Login
    r = await auth_client.post(
        "/login",
        data={"username": "joe", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # Authed: home renders
    r = await auth_client.get("/")
    assert r.status_code == 200
    assert "joe" in r.text

    # Logout
    r = await auth_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    # After logout: home redirects again
    r = await auth_client.get("/", follow_redirects=False)
    assert r.status_code == 303


async def test_already_authed_user_visiting_login_is_redirected(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_user(clean_db, username="joe", password="hunter2")
    await auth_client.post(
        "/login",
        data={"username": "joe", "password": "hunter2"},
        follow_redirects=False,
    )
    r = await auth_client.get("/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
