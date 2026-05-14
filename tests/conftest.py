from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from dotenv import load_dotenv

# Load .env and seed test defaults BEFORE importing the app, because the app
# reads `SESSION_SECRET` at module import time when it constructs the
# SessionMiddleware. `Settings` is `lru_cache`d, so late env-var changes are
# invisible — set what you need here first.
load_dotenv()
os.environ.setdefault("SESSION_SECRET", "test-only-session-secret")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pymongo import AsyncMongoClient  # noqa: E402
from pymongo.asynchronous.database import AsyncDatabase  # noqa: E402

from solisdash.app import app, get_db  # noqa: E402
from solisdash.db import INDEXES, ensure_indexes  # noqa: E402

TEST_DB_PREFIX = "solis_test_"


def _worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def _test_db_name() -> str:
    return f"{TEST_DB_PREFIX}{_worker_id()}"


@pytest.fixture(scope="session")
def mongo_uri() -> str:
    uri = os.environ.get("SOLIS_MONGODB_URI")
    if not uri:
        pytest.skip("SOLIS_MONGODB_URI not set; DB tests skipped")
    return uri


@pytest.fixture(scope="session")
def test_db_name() -> str:
    return _test_db_name()


@pytest.fixture
async def mongo_db(
    mongo_uri: str, test_db_name: str
) -> AsyncIterator[AsyncDatabase[dict[str, Any]]]:
    """Per-test Mongo handle.

    Each test gets its own ``AsyncMongoClient`` because pymongo's async client
    binds to the event loop it is opened on, and pytest-asyncio gives every
    test its own loop. The per-worker DB is dropped once at session end via
    :func:`pytest_sessionfinish`.
    """
    assert test_db_name.startswith(TEST_DB_PREFIX), "refusing to use non-test DB"
    cli: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(mongo_uri)
    db = cli[test_db_name]
    await ensure_indexes(db)
    try:
        yield db
    finally:
        await cli.close()


@pytest.fixture
async def clean_db(
    mongo_db: AsyncDatabase[dict[str, Any]],
) -> AsyncDatabase[dict[str, Any]]:
    """Mongo DB with all known collections emptied before the test runs."""
    for coll_name in INDEXES:
        await mongo_db[coll_name].delete_many({})
    return mongo_db


@pytest.fixture
def client() -> Iterator[TestClient]:
    """TestClient against the real app, with `get_db` left unoverridden.

    Use this for endpoints that don't touch the database (e.g. /health,
    GET /login).
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def auth_client(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> AsyncIterator[httpx.AsyncClient]:
    """Async ASGI client with `get_db` overridden to the per-test database.

    The TestClient (sync) drives requests on a fresh loop, which clashes with
    pymongo's loop-binding once `clean_db` has been opened on the test loop.
    Running the app over ASGITransport keeps every Mongo call on one loop.
    """

    async def _override() -> AsyncDatabase[dict[str, Any]]:
        return clean_db

    app.dependency_overrides[get_db] = _override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_db, None)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Drop this worker's test DB at session end."""
    uri = os.environ.get("SOLIS_MONGODB_URI")
    if not uri:
        return
    name = _test_db_name()
    assert name.startswith(TEST_DB_PREFIX)

    async def _drop() -> None:
        cli: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(uri)
        try:
            await cli.drop_database(name)
        finally:
            await cli.close()

    asyncio.run(_drop())
