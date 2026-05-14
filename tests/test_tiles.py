from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.client import SolisAPIError, SolisClient
from solisdash.tiles import (
    LiveTilesService,
    TTLCache,
    from_sample,
    parse_station_detail,
)

BASE = "https://api.example.invalid:13333"


# --- TTLCache --------------------------------------------------------------


async def test_ttl_cache_returns_cached_value_within_ttl() -> None:
    cache = TTLCache(ttl=60.0)
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return 42

    assert await cache.get_or_set("k", factory) == 42
    assert await cache.get_or_set("k", factory) == 42
    assert calls == 1


async def test_ttl_cache_refreshes_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = TTLCache(ttl=10.0)
    calls = 0
    fake_time = [0.0]
    monkeypatch.setattr("solisdash.tiles.time.monotonic", lambda: fake_time[0])

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_set("k", factory) == 1
    fake_time[0] = 5.0
    assert await cache.get_or_set("k", factory) == 1  # still cached
    fake_time[0] = 20.0
    assert await cache.get_or_set("k", factory) == 2  # past TTL → refetched
    assert calls == 2


async def test_ttl_cache_does_not_cache_exceptions() -> None:
    cache = TTLCache(ttl=60.0)
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await cache.get_or_set("k", factory)
    with pytest.raises(RuntimeError):
        await cache.get_or_set("k", factory)
    assert calls == 2  # both calls hit the factory


async def test_ttl_cache_concurrent_callers_collapse_to_one_factory_call() -> None:
    """Two awaiters for the same key share a single factory invocation."""
    cache = TTLCache(ttl=60.0)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "value"

    task_a = asyncio.create_task(cache.get_or_set("k", factory))
    await started.wait()
    task_b = asyncio.create_task(cache.get_or_set("k", factory))
    await asyncio.sleep(0)
    release.set()
    assert await task_a == "value"
    assert await task_b == "value"
    assert calls == 1


# --- parse_station_detail / from_sample ------------------------------------


def test_parse_station_detail_extracts_tile_fields_with_units() -> None:
    detail = {
        "id": "S1",
        "stationName": "Roof",
        "psum": 4.25,
        "psumStr": "kW",
        "dayEnergy": 31.3,
        "dayEnergyStr": "kWh",
        "monthEnergy": 839.0,
        "monthEnergyStr": "kWh",
        "batteryPercent": 78.0,
        "dataTimestamp": "1687844402978",
    }
    tiles = parse_station_detail(detail, alarm_count=2)
    assert tiles.station_id == "S1"
    assert tiles.station_name == "Roof"
    assert tiles.current_power == pytest.approx(4.25)
    assert tiles.current_power_unit == "kW"
    assert tiles.today_energy == pytest.approx(31.3)
    assert tiles.month_energy == pytest.approx(839.0)
    assert tiles.battery_soc_pct == pytest.approx(78.0)
    assert tiles.alarm_count == 2
    assert tiles.data_ts is not None
    assert tiles.data_ts.tzinfo is UTC
    assert tiles.stale is False


def test_parse_station_detail_tolerates_missing_fields() -> None:
    tiles = parse_station_detail({"id": "S1"}, alarm_count=None)
    assert tiles.station_id == "S1"
    assert tiles.station_name == ""
    assert tiles.current_power is None
    assert tiles.today_energy is None
    assert tiles.month_energy is None
    assert tiles.battery_soc_pct is None
    assert tiles.alarm_count is None
    assert tiles.data_ts is None


def test_parse_station_detail_falls_back_to_power_when_psum_absent() -> None:
    tiles = parse_station_detail(
        {"id": "S1", "power": 9.9, "powerStr": "kW"}, alarm_count=0
    )
    assert tiles.current_power == pytest.approx(9.9)
    assert tiles.current_power_unit == "kW"


def test_from_sample_marks_stale_and_handles_datetime_timestamps() -> None:
    sample = {
        "station_id": "S1",
        "ts": datetime(2026, 5, 14, 9, 30, tzinfo=UTC),
        "power": 3.1,
        "power_unit": "kW",
        "day_energy": 12.0,
        "battery_soc": 42.0,
    }
    tiles = from_sample(sample, station_name="Roof")
    assert tiles.stale is True
    assert tiles.station_id == "S1"
    assert tiles.station_name == "Roof"
    assert tiles.data_ts == datetime(2026, 5, 14, 9, 30, tzinfo=UTC)
    assert tiles.current_power == pytest.approx(3.1)
    assert tiles.today_energy == pytest.approx(12.0)
    assert tiles.battery_soc_pct == pytest.approx(42.0)


# --- LiveTilesService ------------------------------------------------------


class Scripted:
    """Capture requests and return scripted responses, by URL path."""

    def __init__(self, responses: dict[str, list[httpx.Response]]) -> None:
        self._responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        queue = self._responses.get(path) or []
        if not queue:
            raise AssertionError(f"no scripted response for {path!r}")
        return queue.pop(0)


def _envelope(data: Any, code: str = "0") -> dict[str, Any]:
    return {"success": code == "0", "code": code, "msg": "ok", "data": data}


def _page(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "page": {
            "records": records,
            "total": len(records),
            "size": 20,
            "current": 1,
            "pages": 1,
        }
    }


def _make_solis(handler: Callable[[httpx.Request], httpx.Response]) -> SolisClient:
    return SolisClient(
        base_url=BASE,
        key_id="kid",
        key_secret="sec",
        transport=httpx.MockTransport(handler),
        max_retries=0,
        backoff_initial=0.0,
        backoff_max=0.0,
    )


async def test_default_station_id_uses_first_station_when_unset() -> None:
    handler = Scripted({
        "/v1/api/userStationList": [
            httpx.Response(200, json=_envelope(_page([{"id": "S-FIRST"}, {"id": "S-2"}]))),
        ],
    })
    async with _make_solis(handler) as solis:
        service = LiveTilesService(solis=solis, db=_FakeDB())  # type: ignore[arg-type]
        assert await service.default_station_id() == "S-FIRST"

    # Cached: a second call must not re-hit the API.
    assert handler.calls.count("/v1/api/userStationList") == 1


async def test_default_station_id_honors_pinned_setting() -> None:
    handler = Scripted({})
    async with _make_solis(handler) as solis:
        service = LiveTilesService(
            solis=solis, db=_FakeDB(), default_station_id="S-PIN"  # type: ignore[arg-type]
        )
        assert await service.default_station_id() == "S-PIN"
    assert handler.calls == []


async def test_get_tiles_returns_fresh_data_from_solis(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    detail = {
        "id": "S1",
        "stationName": "Roof",
        "psum": 4.0,
        "psumStr": "kW",
        "dayEnergy": 12.0,
        "dayEnergyStr": "kWh",
        "monthEnergy": 100.0,
        "monthEnergyStr": "kWh",
        "batteryPercent": 80,
    }
    handler = Scripted({
        "/v1/api/stationDetail": [httpx.Response(200, json=_envelope(detail))],
        "/v1/api/alarmList": [httpx.Response(200, json=_envelope(_page([{"alarm_code": "x"}])))],
    })
    async with _make_solis(handler) as solis:
        service = LiveTilesService(solis=solis, db=clean_db, default_station_id="S1")
        tiles = await service.get_tiles("S1")
    assert tiles.station_id == "S1"
    assert tiles.current_power == pytest.approx(4.0)
    assert tiles.alarm_count == 1
    assert tiles.stale is False


async def test_get_tiles_caches_for_same_station(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    handler = Scripted({
        "/v1/api/stationDetail": [
            httpx.Response(200, json=_envelope({"id": "S1", "psum": 1.0})),
        ],
        "/v1/api/alarmList": [
            httpx.Response(200, json=_envelope(_page([]))),
        ],
    })
    async with _make_solis(handler) as solis:
        service = LiveTilesService(solis=solis, db=clean_db, default_station_id="S1")
        await service.get_tiles("S1")
        await service.get_tiles("S1")  # second call must hit the cache, not the API
    assert handler.calls.count("/v1/api/stationDetail") == 1


async def test_get_tiles_falls_back_to_station_samples_on_rate_limit(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    # Seed a recent sample for S1.
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await clean_db["station_samples"].insert_many(
        [
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
                "psum": 1.5,
                "day_energy": 5.0,
                "battery_soc": 30.0,
            },
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 14, 9, 0, tzinfo=UTC),
                "psum": 2.5,
                "day_energy": 10.0,
                "battery_soc": 50.0,
            },
        ]
    )

    handler = Scripted({
        "/v1/api/stationDetail": [
            httpx.Response(200, json=_envelope({}, code="1004")),
        ],
    })
    async with _make_solis(handler) as solis:
        service = LiveTilesService(solis=solis, db=clean_db, default_station_id="S1")
        tiles = await service.get_tiles("S1")
    assert tiles.stale is True
    assert tiles.station_name == "Roof"
    # Latest sample wins.
    assert tiles.current_power == pytest.approx(2.5)
    assert tiles.battery_soc_pct == pytest.approx(50.0)
    assert tiles.error is not None and "1004" in tiles.error


async def test_get_tiles_raises_on_rate_limit_without_fallback(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    handler = Scripted({
        "/v1/api/stationDetail": [
            httpx.Response(200, json=_envelope({}, code="1004")),
        ],
    })
    async with _make_solis(handler) as solis:
        service = LiveTilesService(solis=solis, db=clean_db, default_station_id="S1")
        with pytest.raises(SolisAPIError):
            await service.get_tiles("S1")


# Minimal stand-in DB for the no-DB tests above; only used where the code path
# never touches it.
class _FakeDB:
    def __getitem__(self, name: str) -> Any:
        raise AssertionError(f"unexpected DB access for {name!r}")
