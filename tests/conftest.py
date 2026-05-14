from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.app import app
from solisdash.db import INDEXES, ensure_indexes

load_dotenv()

TEST_DB_PREFIX = "solis_test_"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


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
