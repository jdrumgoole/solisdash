"""MongoDB connection and index setup for solisdash.

Schema-by-index: collections are created lazily on first write, so the only
durable schema is the index list. Add a collection here when you need it.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, IndexModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import OperationFailure

DEFAULT_DB_NAME = "solis"

INDEXES: dict[str, list[IndexModel]] = {
    "users": [
        IndexModel([("username", ASCENDING)], unique=True, name="username_unique"),
    ],
    "stations": [
        IndexModel([("id", ASCENDING)], unique=True, name="station_id_unique"),
    ],
    "station_samples": [
        IndexModel([("station_id", ASCENDING), ("ts", ASCENDING)], name="station_ts"),
    ],
    "station_daily": [
        IndexModel(
            [("station_id", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="station_date_unique",
        ),
    ],
    "station_monthly": [
        IndexModel(
            [("station_id", ASCENDING), ("month", ASCENDING)],
            unique=True,
            name="station_month_unique",
        ),
    ],
    "alarms": [
        # Partial unique index — `null`/missing `id` rows from older docs
        # don't collide with each other.
        IndexModel(
            [("id", ASCENDING)],
            unique=True,
            name="alarm_id_unique",
            partialFilterExpression={"id": {"$type": "string"}},
        ),
        IndexModel(
            [("station_id", ASCENDING), ("alarm_begin_time", DESCENDING)],
            name="station_alarm_time",
        ),
        IndexModel([("state", ASCENDING)], name="alarm_state"),
    ],
}


def connect(uri: str) -> AsyncMongoClient[dict[str, Any]]:
    """Open an async client. Caller owns the lifetime — close with `await client.close()`."""
    return AsyncMongoClient(uri)


def get_database(
    client: AsyncMongoClient[dict[str, Any]],
    name: str = DEFAULT_DB_NAME,
) -> AsyncDatabase[dict[str, Any]]:
    return client[name]


async def ensure_indexes(db: AsyncDatabase[dict[str, Any]]) -> None:
    """Create every index in :data:`INDEXES`. Idempotent — safe to call on startup.

    Handles the case where a previously-created index has the same name but a
    different spec (e.g. when this module's `INDEXES` definition has changed
    between deploys). MongoDB raises `IndexKeySpecsConflict` (code 86) for
    that; we drop the user indexes and recreate.
    """
    for collection_name, models in INDEXES.items():
        coll = db[collection_name]
        try:
            await coll.create_indexes(models)
        except OperationFailure as exc:
            if exc.code != 86:  # IndexKeySpecsConflict
                raise
            existing = await coll.index_information()
            for name in existing:
                if name != "_id_":
                    await coll.drop_index(name)
            await coll.create_indexes(models)
