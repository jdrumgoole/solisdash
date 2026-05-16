from __future__ import annotations

from datetime import datetime, timezone
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
                "ts": datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
                "psum": 1.5,
                "power_unit": "kW",
            },
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 13, 12, 5, tzinfo=timezone.utc),
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
    assert "chart.umd.min.js" in body
    assert "chartjs-adapter-date-fns" in body
    # Must be vendored under /static/vendor/ (see test_clickability.py).
    assert "cdn.jsdelivr.net" not in body


async def test_history_page_renders_metric_tabs(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history")
    body = r.text
    for m in ("power", "energy", "battery", "money", "alarms"):
        assert f'data-metric="{m}"' in body


async def test_day_json_supports_battery_metric(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await clean_db["station_samples"].insert_many(
        [
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
                "battery_soc": 22.5,
            },
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
                "battery_soc": 81.0,
            },
        ]
    )
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/day.json?station_id=S1&when=2026-05-13&metric=battery"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "Battery SOC"
    assert body["unit"] == "%"
    assert [p["v"] for p in body["points"]] == [22.5, 81.0]


async def test_day_json_rejects_metric_not_supported_for_view(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """Money is a daily-rollup metric — not valid for the Day view."""
    await _seed_data(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/day.json?station_id=S1&when=2026-05-13&metric=money"
    )
    assert r.status_code == 400


async def test_month_json_supports_money_metric(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await clean_db["station_daily"].insert_many(
        [
            {"station_id": "S1", "date": "2026-05-01", "money": 3.2, "money_unit": "€"},
            {"station_id": "S1", "date": "2026-05-13", "money": 4.5, "money_unit": "€"},
        ]
    )
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/month.json?station_id=S1&month=2026-05&metric=money"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["label"] == "Daily revenue"
    assert body["unit"] == "€"
    assert [(p["t"], p["v"]) for p in body["points"]] == [
        ("2026-05-01", 3.2),
        ("2026-05-13", 4.5),
    ]


async def test_history_page_warns_when_no_stations(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history")
    assert r.status_code == 200
    body = r.text
    assert "No stations stored yet" in body
    # GUI-driven recovery — the empty state must offer a button, never a CLI
    # command. Specifically, do NOT mention `invoke poll-once` or `uv run`.
    assert "invoke poll-once" not in body
    assert "uv run" not in body
    assert 'hx-post="/history/poll-now"' in body
    assert "Poll SolisCloud now" in body


# --- /history/poll-now ------------------------------------------------------


class _FakeSolisClient:
    """SolisClient stand-in for /history/poll-now tests.

    Quacks like the bits of `SolisClient` the `Poller` actually calls:
    `user_station_list` for discovery, `station_detail` per station.
    """

    def __init__(
        self,
        *,
        stations: list[dict[str, Any]] | None = None,
        detail: dict[str, Any] | None = None,
        raise_on_list: BaseException | None = None,
    ) -> None:
        self._stations = stations or []
        self._detail = detail or {}
        self._raise_on_list = raise_on_list

    async def user_station_list(
        self, page_no: int = 1, page_size: int = 20, **_: Any
    ) -> Any:
        from solisdash.client import Page

        if self._raise_on_list is not None:
            raise self._raise_on_list
        return Page(
            records=list(self._stations),
            total=len(self._stations),
            size=page_size,
            current=page_no,
            pages=1,
        )

    async def station_detail(self, *, station_id: Any = None, **_: Any) -> dict[str, Any]:
        return {"id": station_id, "stationName": f"Station {station_id}", **self._detail}


async def test_history_poll_now_requires_auth(
    auth_client: httpx.AsyncClient,
) -> None:
    r = await auth_client.post("/history/poll-now", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_history_poll_now_writes_samples_and_asks_for_refresh(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    from solisdash.app import app, get_solis_client

    fake = _FakeSolisClient(stations=[{"id": "S1"}, {"id": "S2"}])

    async def _override_solis() -> _FakeSolisClient:
        return fake

    app.dependency_overrides[get_solis_client] = _override_solis
    try:
        await _login(auth_client, clean_db)
        r = await auth_client.post("/history/poll-now")
        assert r.status_code == 200
        assert r.headers.get("hx-refresh") == "true"
        assert "Polled 2 station" in r.text
        # The samples are durable so subsequent /history renders pick them up.
        assert await clean_db["station_samples"].count_documents({}) == 2
        assert await clean_db["stations"].count_documents({}) == 2
    finally:
        app.dependency_overrides.pop(get_solis_client, None)


async def test_history_poll_now_renders_error_on_soliscloud_failure(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    from solisdash.app import app, get_solis_client
    from solisdash.client import SolisAPIError

    fake = _FakeSolisClient(
        raise_on_list=SolisAPIError(code="1004", msg="wrong sign"),
    )

    async def _override_solis() -> _FakeSolisClient:
        return fake

    app.dependency_overrides[get_solis_client] = _override_solis
    try:
        await _login(auth_client, clean_db)
        r = await auth_client.post("/history/poll-now")
        assert r.status_code == 200
        assert "SolisCloud rejected" in r.text
        assert "wrong sign" in r.text
        assert "/settings" in r.text  # nudge to fix creds
    finally:
        app.dependency_overrides.pop(get_solis_client, None)


async def test_history_poll_now_warns_when_account_has_no_stations(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    from solisdash.app import app, get_solis_client

    fake = _FakeSolisClient(stations=[])

    async def _override_solis() -> _FakeSolisClient:
        return fake

    app.dependency_overrides[get_solis_client] = _override_solis
    try:
        await _login(auth_client, clean_db)
        r = await auth_client.post("/history/poll-now")
        assert r.status_code == 200
        assert "no stations" in r.text.lower()
        assert "hx-refresh" not in {k.lower() for k in r.headers}
    finally:
        app.dependency_overrides.pop(get_solis_client, None)


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
