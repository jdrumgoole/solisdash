from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.auth import create_user


async def _login(
    ac: httpx.AsyncClient, db: AsyncDatabase[dict[str, Any]]
) -> None:
    await create_user(db, username="joe", password="hunter2", role="admin")
    r = await ac.post(
        "/login",
        data={"username": "joe", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303


# --- CSV exports -----------------------------------------------------------


async def _seed_history(db: AsyncDatabase[dict[str, Any]]) -> None:
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
        ]
    )


async def test_day_csv_returns_csv_with_attachment_header(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_history(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/day.csv?station_id=S1&when=2026-05-13"
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().split("\n")
    assert lines[0] == "timestamp_ms,power (kW)"
    # Two data rows for the seeded samples.
    assert len(lines) == 3


async def test_month_csv_returns_one_row_per_day(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_history(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/month.csv?station_id=S1&month=2026-05"
    )
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "date,energy (kWh)"
    assert lines[1] == "2026-05-01,10.0"
    assert lines[2] == "2026-05-13,12.0"


async def test_year_csv_aggregates_to_monthly(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_history(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/year.csv?station_id=S1&year=2026")
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "month,energy (kWh)"
    # 10.0 + 12.0 from the two seeded May rows
    assert lines[1] == "2026-05,22.0"


async def test_all_csv_aggregates_to_yearly(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_history(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/all.csv?station_id=S1")
    assert r.status_code == 200
    lines = r.text.strip().split("\n")
    assert lines[0] == "year,energy (kWh)"
    assert lines[1] == "2026,22.0"


async def test_csv_endpoints_require_auth(
    auth_client: httpx.AsyncClient,
) -> None:
    """auth_client overrides get_db / get_tiles_service but no session set."""
    r = await auth_client.get(
        "/history/day.csv?station_id=S1&when=2026-05-13",
        follow_redirects=False,
    )
    assert r.status_code == 303


# --- Alarms page -----------------------------------------------------------


async def _seed_alarms(db: AsyncDatabase[dict[str, Any]]) -> None:
    await db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    polled = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    await db["alarms"].insert_many(
        [
            {
                "id": "A1",
                "station_id": "S1",
                "alarm_code": "2129",
                "alarm_device_sn": "ABC",
                "alarm_begin_time": 1_700_000_000_000,
                "state": "0",
                "alarm_msg": "Inverter offline",
                "polled_at": polled,
            },
            {
                "id": "A2",
                "station_id": "S1",
                "alarm_code": "2130",
                "alarm_device_sn": "ABC",
                "alarm_begin_time": 1_700_000_001_000,
                "state": "1",
                "alarm_msg": "Recovered",
                "polled_at": polled,
            },
        ]
    )


async def test_alarms_page_requires_auth(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.get("/alarms", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_alarms_page_lists_rows(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_alarms(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/alarms")
    assert r.status_code == 200
    body = r.text
    assert "2129" in body
    assert "Inverter offline" in body
    assert "pending" in body


async def test_alarms_page_filters_by_state(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _seed_alarms(clean_db)
    await _login(auth_client, clean_db)
    r = await auth_client.get("/alarms?state=0")
    assert r.status_code == 200
    assert "2129" in r.text
    assert "2130" not in r.text


async def test_alarms_page_shows_empty_state(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.get("/alarms")
    assert r.status_code == 200
    assert "No alarms match these filters" in r.text


# --- /ready ----------------------------------------------------------------


async def test_ready_returns_503_when_mongo_ping_fails(
    auth_client: httpx.AsyncClient,
) -> None:
    """Override `get_db` with a stub whose ping raises."""
    from solisdash.app import app, get_db

    class _BadDB:
        async def command(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("simulated outage")

        def __getitem__(self, _name: str) -> Any:
            raise AssertionError("not reached in this test")

    async def _override() -> Any:
        return _BadDB()

    app.dependency_overrides[get_db] = _override
    try:
        r = await auth_client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["ready"] is False
        assert body["checks"]["mongo"]["ok"] is False
        assert "simulated outage" in body["checks"]["mongo"]["detail"]
    finally:
        # auth_client's fixture teardown restores its own override.
        pass


async def test_ready_returns_200_when_mongo_pings_and_scheduler_disabled(
    auth_client: httpx.AsyncClient,
) -> None:
    """With default RUN_SCHEDULER=false, only a Mongo ping is required."""
    r = await auth_client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"]["mongo"]["ok"] is True
    assert body["checks"]["scheduler"]["ok"] is True


# --- Dark-mode toggle ------------------------------------------------------


async def test_base_template_includes_theme_toggle(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.get("/")
    assert r.status_code == 200
    assert 'id="theme-toggle"' in r.text
    assert "solisdash-theme" in r.text  # localStorage key referenced


async def test_health_remains_simple(auth_client: httpx.AsyncClient) -> None:
    """`/health` is the liveness probe and stays cheap: no DB, no auth."""
    r = await auth_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
