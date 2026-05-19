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

# Collections the /data Purge button is allowed to drop.
#
# Intentionally NARROW: only collections whose contents can be fully
# re-downloaded from SolisCloud no matter how old they are. Point-in-time
# polled data (`station_samples`, sample-by-sample SOC / power / battery
# at 5-min cadence) is excluded — SolisCloud's `stationDay` endpoint only
# retains recent days, so samples older than that window are
# irrecoverable once purged. Alarms have unclear upstream retention and
# are excluded for the same safety-first reason.
#
# `users` (admin accounts) and the `solisdash.toml` config are local
# state, also never touched.
SOLISCLOUD_COLLECTIONS: tuple[str, ...] = (
    "station_daily",     # stationMonth (one row per day) — fully re-pullable for any past month
    "stations",          # userStationList + stationDetail — re-pullable any time
)

INDEXES: dict[str, list[IndexModel]] = {
    "users": [
        IndexModel([("username", ASCENDING)], unique=True, name="username_unique"),
    ],
    "stations": [
        IndexModel([("id", ASCENDING)], unique=True, name="station_id_unique"),
    ],
    "station_samples": [
        # `unique` so the stationDay intraday backfill is idempotent —
        # re-running a backfill upserts the same per-5-minute points
        # without duplicating them. Real samples always carry
        # `station_id` (string) and `ts` (date); the query planner won't
        # use a partial-filter index for indexed range scans without
        # explicit $type clauses on every query, so we keep this plain
        # `unique`. `ensure_indexes` handles drop-and-recreate when an
        # older non-unique copy exists.
        IndexModel(
            [("station_id", ASCENDING), ("ts", ASCENDING)],
            unique=True,
            name="station_ts_unique",
        ),
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
