from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.auth import create_user


async def _login(
    ac: httpx.AsyncClient, db: AsyncDatabase[dict[str, Any]]
) -> None:
    """Seed an admin user and sign them in on the given client."""
    await create_user(db, username="joe", password="hunter2", role="admin")
    r = await ac.post(
        "/login",
        data={"username": "joe", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303


async def _seed_data(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await db["station_samples"].insert_many(
        [
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
                "psum": 1.5,
                "power_unit": "kW",
            },
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 13, 12, 5, tzinfo=UTC),
                "psum": 2.5,
                "power_unit": "kW",
            },
        ]
    )
    await db["station_daily"].insert_many(
        [
            {"station_id": "S1", "date": "2026-05-01", "energy": 10.0, "energy_unit": "kWh"},
            {"station_id": "S1", "date": "2026-05-13", "energy": 12.0, "energy_unit": "kWh"},
            {"station_id": "S1", "date": "2025-04-01", "energy": 7.0, "energy_unit": "kWh"},
        ]
    )


# --- HTML page -------------------------------------------------------------


async def test_history_page_requires_auth() -> None:
    from solisdash.app import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        r = await ac.get("/history", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_history_page_renders_station_picker_and_chart_canvas(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history")
    assert r.status_code == 200
    body = r.text
    assert 'id="station-select"' in body
    assert "Roof" in body
    assert 'id="history-chart"' in body
    assert "chart.js" in body
    assert "chartjs-adapter-date-fns" in body


async def test_history_page_warns_when_no_stations(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history")
    assert r.status_code == 200
    assert "No stations stored yet" in r.text


# --- JSON endpoints --------------------------------------------------------


async def test_day_json_returns_series_points(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/day.json?station_id=S1&when=2026-05-13")
    assert r.status_code == 200
    body = r.json()
    assert body["station_id"] == "S1"
    assert body["label"] == "Power"
    assert body["unit"] == "kW"
    assert [p["v"] for p in body["points"]] == [1.5, 2.5]


async def test_day_json_400s_on_bad_date(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/day.json?station_id=S1&when=not-a-date")
    assert r.status_code == 400


async def test_day_json_defaults_station_to_first_seen(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/day.json?when=2026-05-13")
    body = r.json()
    assert body["station_id"] == "S1"


async def test_month_json_returns_daily_rows_in_month(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/month.json?station_id=S1&month=2026-05")
    body = r.json()
    assert [(p["t"], p["v"]) for p in body["points"]] == [
        ("2026-05-01", 10.0),
        ("2026-05-13", 12.0),
    ]


async def test_month_json_400s_on_bad_month(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/month.json?station_id=S1&month=2026-99")
    assert r.status_code == 400


async def test_year_json_aggregates_daily_to_monthly(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/year.json?station_id=S1&year=2026")
    body = r.json()
    assert [(p["t"], p["v"]) for p in body["points"]] == [("2026-05", 22.0)]


async def test_all_json_aggregates_daily_to_yearly(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/all.json?station_id=S1")
    body = r.json()
    rows = {p["t"]: p["v"] for p in body["points"]}
    assert rows == {"2025": 7.0, "2026": 22.0}


async def test_json_endpoint_returns_empty_when_no_stations(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/day.json?when=2026-05-13")
    assert r.status_code == 200
    body = r.json()
    assert body["station_id"] is None
    assert body["points"] == []


@pytest.mark.parametrize(
    "url",
    [
        "/history/day.json?when=2026-05-13",
        "/history/month.json?month=2026-05",
        "/history/year.json?year=2026",
        "/history/all.json",
    ],
)
async def test_json_endpoints_require_auth(
    url: str, auth_client: httpx.AsyncClient
) -> None:
    """auth_client overrides get_db/get_tiles_service but no session is set."""
    r = await auth_client.get(url, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
