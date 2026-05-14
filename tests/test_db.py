from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from solisdash.db import INDEXES, ensure_indexes


async def test_ensure_indexes_creates_expected_index_keys(
    mongo_db: AsyncDatabase[dict[str, Any]],
) -> None:
    expected = {
        "users": {"_id_", "username_unique"},
        "stations": {"_id_", "station_id_unique"},
        "station_samples": {"_id_", "station_ts"},
        "station_daily": {"_id_", "station_date_unique"},
        "station_monthly": {"_id_", "station_month_unique"},
        "alarms": {"_id_", "station_alarm_time", "alarm_state"},
    }
    for coll_name, names in expected.items():
        info = await mongo_db[coll_name].index_information()
        assert set(info) == names, f"{coll_name}: {set(info)} != {names}"


async def test_ensure_indexes_is_idempotent(mongo_db: AsyncDatabase[dict[str, Any]]) -> None:
    await ensure_indexes(mongo_db)
    await ensure_indexes(mongo_db)  # second call must not raise
    info = await mongo_db["station_samples"].index_information()
    assert "station_ts" in info


async def test_unique_indexes_have_unique_flag(mongo_db: AsyncDatabase[dict[str, Any]]) -> None:
    for coll_name, models in INDEXES.items():
        info = await mongo_db[coll_name].index_information()
        for model in models:
            doc = model.document
            name = doc["name"]
            is_unique = doc.get("unique", False)
            assert info[name].get("unique", False) == is_unique, f"{coll_name}.{name}"


async def test_users_username_unique_rejects_duplicates(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await clean_db["users"].insert_one(
        {"username": "joe", "password_hash": "x", "role": "admin"}
    )
    with pytest.raises(DuplicateKeyError):
        await clean_db["users"].insert_one(
            {"username": "joe", "password_hash": "y", "role": "user"}
        )


async def test_stations_id_unique_rejects_duplicates(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    with pytest.raises(DuplicateKeyError):
        await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof2"})


async def test_station_daily_composite_unique(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await clean_db["station_daily"].insert_one(
        {"station_id": "S1", "date": "2026-05-13", "energy": 12.3}
    )
    # Same (station_id, date) — must reject
    with pytest.raises(DuplicateKeyError):
        await clean_db["station_daily"].insert_one(
            {"station_id": "S1", "date": "2026-05-13", "energy": 99.9}
        )
    # Different date — must accept
    await clean_db["station_daily"].insert_one(
        {"station_id": "S1", "date": "2026-05-14", "energy": 5.0}
    )


async def test_station_samples_supports_indexed_time_range_query(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    coll = clean_db["station_samples"]
    base = datetime(2026, 5, 13, 0, 0, tzinfo=UTC)
    docs = [
        {"station_id": "S1", "ts": base.replace(hour=h), "power": 100.0 + h}
        for h in range(0, 24)
    ]
    docs.append({"station_id": "S2", "ts": base, "power": 999.0})
    await coll.insert_many(docs)

    start = base.replace(hour=8)
    end = base.replace(hour=12)
    cursor = coll.find({"station_id": "S1", "ts": {"$gte": start, "$lt": end}}).sort("ts", 1)
    found = [doc async for doc in cursor]
    assert [d["ts"].hour for d in found] == [8, 9, 10, 11]

    explain = await clean_db.command(
        "explain",
        {
            "find": "station_samples",
            "filter": {"station_id": "S1", "ts": {"$gte": start, "$lt": end}},
        },
        verbosity="queryPlanner",
    )
    stages = str(explain.get("queryPlanner", {}).get("winningPlan", {}))
    assert "IXSCAN" in stages, f"expected indexed scan, got: {stages}"


async def test_alarms_state_filter_is_indexed(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    coll = clean_db["alarms"]
    await coll.insert_many(
        [
            {"station_id": "S1", "alarm_begin_time": 1, "state": "0", "alarm_code": "2129"},
            {"station_id": "S1", "alarm_begin_time": 2, "state": "1", "alarm_code": "2130"},
            {"station_id": "S2", "alarm_begin_time": 3, "state": "0", "alarm_code": "2131"},
        ]
    )
    pending = [doc async for doc in coll.find({"state": "0"}).sort("alarm_begin_time", -1)]
    assert {d["alarm_code"] for d in pending} == {"2129", "2131"}
