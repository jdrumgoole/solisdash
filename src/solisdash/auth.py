"""Password hashing, user CRUD, session helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pymongo.asynchronous.database import AsyncDatabase

ROLES = ("admin", "user")


def hash_password(password: str) -> str:
    """Return a bcrypt hash of `password`, encoded as utf-8 string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of a plaintext password against a stored hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


async def create_user(
    db: AsyncDatabase[dict[str, Any]],
    *,
    username: str,
    password: str,
    role: str = "user",
) -> dict[str, Any]:
    """Insert a new user with a hashed password. Caller catches `DuplicateKeyError`."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    doc = {
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db["users"].insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


async def find_user(
    db: AsyncDatabase[dict[str, Any]], username: str
) -> dict[str, Any] | None:
    return await db["users"].find_one({"username": username})


async def authenticate(
    db: AsyncDatabase[dict[str, Any]], username: str, password: str
) -> dict[str, Any] | None:
    """Return the user doc if credentials are valid, else None."""
    user = await find_user(db, username)
    if user is None:
        return None
    if not verify_password(password, user.get("password_hash", "")):
        return None
    return user


# --- FastAPI dependencies --------------------------------------------------


def get_current_user(request: Request) -> dict[str, Any] | None:
    """The user dict stored on the session, or None if unauthenticated."""
    user = request.session.get("user")
    return user if isinstance(user, dict) else None


def require_user(
    request: Request,
    user: dict[str, Any] | None = Depends(get_current_user),
) -> dict[str, Any]:
    """Dependency that demands an authenticated session.

    HTML clients get a 303 redirect to /login; API clients (Accept: application/json
    or HX-Request) get a 401 so HTMX swaps can react.
    """
    if user is not None:
        return user
    accept = request.headers.get("accept", "")
    if "application/json" in accept or request.headers.get("hx-request"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"location": "/login"},
    )


def session_login(request: Request, user: dict[str, Any]) -> None:
    """Stash a minimal user record on the session cookie."""
    request.session["user"] = {
        "username": user["username"],
        "role": user.get("role", "user"),
    }


def session_logout(request: Request) -> None:
    request.session.pop("user", None)


def redirect_to(location: str) -> RedirectResponse:
    """303 redirect — safe to use after a form POST."""
    return RedirectResponse(url=location, status_code=status.HTTP_303_SEE_OTHER)
