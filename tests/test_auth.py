from __future__ import annotations

from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from solisdash.auth import (
    ROLES,
    authenticate,
    create_user,
    find_user,
    hash_password,
    verify_password,
)


def test_hash_password_returns_string_distinct_from_plaintext() -> None:
    h = hash_password("hunter2")
    assert isinstance(h, str)
    assert h != "hunter2"
    assert h.startswith("$2")  # bcrypt prefix


def test_verify_password_accepts_correct_password() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True


def test_verify_password_rejects_wrong_password() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter3", h) is False


def test_verify_password_rejects_garbage_hash() -> None:
    assert verify_password("hunter2", "not-a-bcrypt-hash") is False


def test_hash_is_salted_per_call() -> None:
    h1 = hash_password("hunter2")
    h2 = hash_password("hunter2")
    assert h1 != h2  # salts differ
    assert verify_password("hunter2", h1)
    assert verify_password("hunter2", h2)


async def test_create_user_inserts_with_hashed_password(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    user = await create_user(clean_db, username="joe", password="hunter2", role="admin")
    assert user["username"] == "joe"
    assert user["role"] == "admin"
    assert "_id" in user
    assert user["password_hash"] != "hunter2"
    assert user["password_hash"].startswith("$2")

    stored = await clean_db["users"].find_one({"username": "joe"})
    assert stored is not None
    assert stored["password_hash"] == user["password_hash"]


async def test_create_user_rejects_unknown_role(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError, match="role must be one of"):
        await create_user(clean_db, username="joe", password="x", role="superuser")


async def test_create_user_duplicate_username_raises_duplicate_key(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await create_user(clean_db, username="joe", password="hunter2", role="admin")
    with pytest.raises(DuplicateKeyError):
        await create_user(clean_db, username="joe", password="other", role="user")


async def test_find_user_returns_none_for_unknown(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    assert await find_user(clean_db, "nobody") is None


async def test_authenticate_accepts_correct_credentials(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await create_user(clean_db, username="joe", password="hunter2", role="admin")
    user = await authenticate(clean_db, "joe", "hunter2")
    assert user is not None
    assert user["username"] == "joe"


async def test_authenticate_rejects_wrong_password(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await create_user(clean_db, username="joe", password="hunter2", role="admin")
    assert await authenticate(clean_db, "joe", "wrong") is None


async def test_authenticate_rejects_unknown_user(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    assert await authenticate(clean_db, "nobody", "anything") is None


def test_roles_constant_includes_admin_and_user() -> None:
    assert "admin" in ROLES
    assert "user" in ROLES
