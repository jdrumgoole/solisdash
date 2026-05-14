from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.client import SolisAPIError, SolisClient
from solisdash.poller import (
    Poller,
    _detail_to_sample,
    _row_to_daily,
    iter_months,
)

BASE = "https://api.example.invalid:13333"


# --- helpers ---------------------------------------------------------------


def _envelope(data: Any, code: str = "0") -> dict[str, Any]:
    return {"success": code == "0", "code": code, "msg": "ok", "data": data}


def _page(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "page": {
            "records": records,
            "total": len(records),
            "size": 100,
            "current": 1,
            "pages": 1,
        }
    }


class Scripted:
    def __init__(self, responses: dict[str, list[httpx.Response]]) -> None:
        self._responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        queue = self._responses.get(request.url.path) or []
        if not queue:
            raise AssertionError(f"no scripted response for {request.url.path!r}")
        return queue.pop(0)


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


# --- iter_months -----------------------------------------------------------


def test_iter_months_inclusive_same_month() -> None:
    assert list(iter_months(date(2026, 5, 1), date(2026, 5, 31))) == ["2026-05"]


def test_iter_months_spans_multiple_months_inclusive() -> None:
    assert list(iter_months(date(2026, 1, 15), date(2026, 4, 5))) == [
        "2026-01",
        "2026-02",
        "2026-03",
        "2026-04",
    ]


def test_iter_months_wraps_year_boundary() -> None:
    assert list(iter_months(date(2025, 11, 1), date(2026, 2, 28))) == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]


def test_iter_months_rejects_end_before_start() -> None:
    with pytest.raises(ValueError):
        list(iter_months(date(2026, 5, 1), date(2026, 1, 1)))


# --- projectors ------------------------------------------------------------


def test_detail_to_sample_uses_solis_timestamp_when_present() -> None:
    polled = datetime(2026, 5, 14, 12, 30, tzinfo=UTC)
    detail = {
        "psum": 0.76,
        "psumStr": "kW",
        "dayEnergy": 7.3,
        "dayEnergyStr": "kWh",
        "monthEnergy": 200.3,
        "monthEnergyStr": "kWh",
        "batteryPercent": 55,
        "dataTimestamp": "1778760339000",
    }
    sample = _detail_to_sample("S1", detail, polled)
    assert sample["station_id"] == "S1"
    assert sample["psum"] == pytest.approx(0.76)
    assert sample["day_energy"] == pytest.approx(7.3)
    assert sample["month_energy"] == pytest.approx(200.3)
    assert sample["battery_soc"] == pytest.approx(55.0)
    assert sample["ts"] == datetime.fromtimestamp(1778760339, tz=UTC)
    assert sample["polled_at"] == polled


def test_detail_to_sample_falls_back_to_polled_at_when_no_timestamp() -> None:
    polled = datetime(2026, 5, 14, 12, 30, tzinfo=UTC)
    sample = _detail_to_sample("S1", {"psum": 1.0}, polled)
    assert sample["ts"] == polled


def test_row_to_daily_uses_date_str_when_present() -> None:
    row = {
        "dateStr": "2026-05-13",
        "energy": 31.3,
        "energyStr": "kWh",
        "money": 4.2,
        "moneyStr": "EUR",
        "fullHour": 2.6,
    }
    doc = _row_to_daily("S1", row)
    assert doc is not None
    assert doc["station_id"] == "S1"
    assert doc["date"] == "2026-05-13"
    assert doc["energy"] == pytest.approx(31.3)
    assert doc["money"] == pytest.approx(4.2)


def test_row_to_daily_converts_ms_timestamp_to_iso_date() -> None:
    # 2026-05-13 00:00 UTC
    row = {"date": 1778688000000, "energy": 31.3, "energyStr": "kWh"}
    doc = _row_to_daily("S1", row)
    assert doc is not None
    assert doc["date"] == "2026-05-13"


def test_row_to_daily_returns_none_when_no_date() -> None:
    assert _row_to_daily("S1", {"energy": 10}) is None


# --- Poller end-to-end against a clean Mongo + scripted SolisCloud ---------


async def test_poll_current_writes_sample_and_upserts_station(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    detail = {
        "id": "S1",
        "stationName": "Roof",
        "addr": "Dublin",
        "psum": 0.76,
        "psumStr": "kW",
        "dayEnergy": 7.3,
        "dayEnergyStr": "kWh",
        "monthEnergy": 200.3,
        "monthEnergyStr": "kWh",
        "batteryPercent": 55,
        "dataTimestamp": "1778760339000",
    }
    handler = Scripted({
        "/v1/api/stationDetail": [httpx.Response(200, json=_envelope(detail))],
    })
    async with _make_solis(handler) as solis:
        poller = Poller(solis=solis, db=clean_db)
        sample = await poller.poll_current("S1")

    assert sample is not None
    written = [doc async for doc in clean_db["station_samples"].find({"station_id": "S1"})]
    assert len(written) == 1
    assert written[0]["psum"] == pytest.approx(0.76)
    assert written[0]["battery_soc"] == pytest.approx(55.0)

    station = await clean_db["stations"].find_one({"id": "S1"})
    assert station is not None
    assert station["stationName"] == "Roof"
    assert station["addr"] == "Dublin"


async def test_poll_current_returns_none_and_writes_nothing_on_api_error(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    handler = Scripted({
        "/v1/api/stationDetail": [
            httpx.Response(200, json=_envelope({}, code="B0049")),
        ],
    })
    async with _make_solis(handler) as solis:
        poller = Poller(solis=solis, db=clean_db)
        result = await poller.poll_current("S1")
    assert result is None
    count = await clean_db["station_samples"].count_documents({"station_id": "S1"})
    assert count == 0


async def test_poll_current_all_iterates_stations(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    handler = Scripted({
        "/v1/api/userStationList": [
            httpx.Response(200, json=_envelope(_page([{"id": "A"}, {"id": "B"}]))),
        ],
        "/v1/api/stationDetail": [
            httpx.Response(200, json=_envelope({"id": "A", "psum": 1.0})),
            httpx.Response(200, json=_envelope({"id": "B", "psum": 2.0})),
        ],
    })
    async with _make_solis(handler) as solis:
        poller = Poller(solis=solis, db=clean_db)
        n = await poller.poll_current_all()
    assert n == 2
    a = await clean_db["station_samples"].find_one({"station_id": "A"})
    b = await clean_db["station_samples"].find_one({"station_id": "B"})
    assert a is not None and a["psum"] == pytest.approx(1.0)
    assert b is not None and b["psum"] == pytest.approx(2.0)


async def test_poll_daily_for_month_upserts_each_row(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    handler = Scripted({
        "/v1/api/stationMonth": [
            httpx.Response(
                200,
                json=_envelope(
                    [
                        {"dateStr": "2026-05-01", "energy": 10, "energyStr": "kWh"},
                        {"dateStr": "2026-05-02", "energy": 12, "energyStr": "kWh"},
                    ]
                ),
            ),
        ],
    })
    async with _make_solis(handler) as solis:
        poller = Poller(solis=solis, db=clean_db)
        written = await poller.poll_daily_for_month("S1", "2026-05")
    assert written == 2
    cursor = clean_db["station_daily"].find({"station_id": "S1"}).sort("date", 1)
    rows = [doc async for doc in cursor]
    assert [r["date"] for r in rows] == ["2026-05-01", "2026-05-02"]
    assert [r["energy"] for r in rows] == [10.0, 12.0]


async def test_poll_daily_for_month_idempotent(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    payload = {
        "/v1/api/stationMonth": [
            httpx.Response(
                200,
                json=_envelope([{"dateStr": "2026-05-01", "energy": 10}]),
            ),
            httpx.Response(
                200,
                json=_envelope([{"dateStr": "2026-05-01", "energy": 11}]),
            ),
        ],
    }
    handler = Scripted(payload)
    async with _make_solis(handler) as solis:
        poller = Poller(solis=solis, db=clean_db)
        await poller.poll_daily_for_month("S1", "2026-05")
        await poller.poll_daily_for_month("S1", "2026-05")
    rows = [doc async for doc in clean_db["station_daily"].find({"station_id": "S1"})]
    assert len(rows) == 1  # upserted, not duplicated
    assert rows[0]["energy"] == pytest.approx(11.0)  # latest value wins


async def test_backfill_daily_iterates_months_and_stations(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    handler = Scripted({
        "/v1/api/userStationList": [
            httpx.Response(200, json=_envelope(_page([{"id": "S1"}, {"id": "S2"}]))),
        ],
        "/v1/api/stationMonth": [
            httpx.Response(200, json=_envelope([{"dateStr": "2026-04-30", "energy": 1}])),
            httpx.Response(200, json=_envelope([{"dateStr": "2026-05-01", "energy": 2}])),
            httpx.Response(200, json=_envelope([{"dateStr": "2026-04-30", "energy": 3}])),
            httpx.Response(200, json=_envelope([{"dateStr": "2026-05-01", "energy": 4}])),
        ],
    })
    async with _make_solis(handler) as solis:
        poller = Poller(solis=solis, db=clean_db)
        counts = await poller.backfill_daily(
            start=date(2026, 4, 1), end=date(2026, 5, 1)
        )
    assert counts == {"S1": 2, "S2": 2}
    total = await clean_db["station_daily"].count_documents({})
    assert total == 4


async def test_poll_daily_for_month_returns_zero_on_solis_error(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    handler = Scripted({
        "/v1/api/stationMonth": [
            httpx.Response(200, json=_envelope({}, code="1004")),
        ],
    })
    async with _make_solis(handler) as solis:
        poller = Poller(solis=solis, db=clean_db)
        written = await poller.poll_daily_for_month("S1", "2026-05")
    assert written == 0


def test_solis_api_error_imported_for_callers() -> None:
    """Sanity-check that the public symbols stay importable."""
    assert SolisAPIError("X", "y").code == "X"
