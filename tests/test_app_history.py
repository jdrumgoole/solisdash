from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.auth import create_user


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the Poller's TokenBucket throttle for the duration of these
    tests. The bucket exists to be polite to SolisCloud; it has no place
    making integration tests wait one-second between mocked API calls."""
    from solisdash.ratelimit import TokenBucket

    async def _no_op(self: TokenBucket) -> None:
        return None

    monkeypatch.setattr(TokenBucket, "acquire", _no_op)


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
    # Tab strip after the v0.9 aggressive prune: 11 visible tabs.
    # Money / Total / Alarms tabs were pruned (Cashflow supersedes Money
    # once tariffs are set; Total was duplicate of Energy; the /alarms
    # nav page covers alarms).
    visible = {
        "energy", "power",
        "battery", "battery_power", "battery_charge", "battery_discharge",
        "consumption", "import_energy", "export_energy", "net",
        "cashflow",
    }
    for m in visible:
        assert f'data-metric="{m}"' in body, f"expected tab {m} missing"
    for m in ("money", "total_output", "alarms"):
        assert f'data-metric="{m}"' not in body, f"pruned tab {m} still present"
    assert 'id="range-start"' in body
    assert 'id="range-end"' in body
    for preset in ("today", "month", "year", "all"):
        assert f'data-preset="{preset}"' in body


# --- /history/range.json (auto-resolution) -------------------------------


async def test_range_json_returns_daily_for_short_span_energy(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """Sub-month Energy range → daily totals from station_daily."""
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await clean_db["station_daily"].insert_many(
        [
            {"station_id": "S1", "date": "2026-05-01", "energy": 10.0, "energy_unit": "kWh"},
            {"station_id": "S1", "date": "2026-05-13", "energy": 12.0, "energy_unit": "kWh"},
        ]
    )
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/range.json?station_id=S1&start=2026-05-01&end=2026-05-31&metric=energy"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolution"] == "daily totals"
    assert [(p["t"], p["v"]) for p in body["points"]] == [
        ("2026-05-01", 10.0),
        ("2026-05-13", 12.0),
    ]


async def test_range_json_aggregates_monthly_for_multimonth_span(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """Span > 31 days → monthly aggregation, one bar per YYYY-MM."""
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await clean_db["station_daily"].insert_many(
        [
            {"station_id": "S1", "date": "2026-04-15", "energy": 5.0},
            {"station_id": "S1", "date": "2026-04-16", "energy": 6.0},
            {"station_id": "S1", "date": "2026-05-01", "energy": 4.0},
        ]
    )
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/range.json?station_id=S1&start=2026-01-01&end=2026-06-30&metric=energy"
    )
    body = r.json()
    assert body["resolution"] == "monthly totals"
    rows = {p["t"]: p["v"] for p in body["points"]}
    assert rows == {"2026-04": 11.0, "2026-05": 4.0}


async def test_range_json_downsamples_sample_metric_aligned_with_rollups(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """For spans > 7 days, sample-only metrics (Power, Battery SOC, etc.)
    bucket the same way as daily-rollup metrics do:
      * 8-31 days  → daily averages
      * 32-732     → monthly averages
      * > 732      → yearly averages
    Keeps the x-axis consistent when the user flips tabs."""
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await clean_db["station_samples"].insert_many(
        [
            {"station_id": "S1",
             "ts": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
             "battery_soc": 40.0},
            {"station_id": "S1",
             "ts": datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc),
             "battery_soc": 60.0},
            {"station_id": "S1",
             "ts": datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
             "battery_soc": 80.0},
        ]
    )
    await _login(auth_client, clean_db)

    # 30-day range — auto must pick daily averages, two daily points.
    r = await auth_client.get(
        "/history/range.json?station_id=S1&start=2026-04-15&end=2026-05-14&metric=battery"
    )
    body = r.json()
    assert body["resolution"] == "daily averages"
    rows = {p["t"]: p["v"] for p in body["points"]}
    assert rows == {"2026-05-01": 50.0, "2026-05-02": 80.0}

    # 60-day range — auto must pick monthly averages.
    r = await auth_client.get(
        "/history/range.json?station_id=S1&start=2026-04-01&end=2026-05-31&metric=battery"
    )
    body = r.json()
    assert body["resolution"] == "monthly averages"
    rows = {p["t"]: p["v"] for p in body["points"]}
    # Single bucket May 2026 → mean of (40, 60, 80) = 60.
    assert rows == {"2026-05": 60.0}


async def test_range_json_uses_samples_for_short_span_sample_metric(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """≤ 7 day spans still get raw 5-minute samples (line chart)."""
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/range.json?station_id=S1&start=2026-05-13&end=2026-05-13&metric=battery"
    )
    body = r.json()
    assert body["resolution"] == "5-min samples"


async def test_range_json_handles_battery_power_metric(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """battery_power is sample-only; missing it from auto_range's
    sample-metric tuple used to cause a 500 on the Battery power tab."""
    await clean_db["stations"].insert_one({"id": "S1", "stationName": "Roof"})
    await clean_db["station_samples"].insert_many(
        [
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
                "battery_power": -1.2,
                "battery_power_unit": "kW",
            },
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 17, 10, 5, tzinfo=timezone.utc),
                "battery_power": 0.8,
                "battery_power_unit": "kW",
            },
        ]
    )
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/range.json?station_id=S1&start=2026-05-17&end=2026-05-17&metric=battery_power"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolution"] == "5-min samples"
    assert body["label"] == "Battery power"
    assert body["unit"] == "kW"
    assert [p["v"] for p in body["points"]] == [-1.2, 0.8]


async def test_range_json_rejects_bad_date(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.get(
        "/history/range.json?station_id=S1&start=nope&end=2026-05-31&metric=energy"
    )
    assert r.status_code == 400


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
    # Poll-now lives on the Data tab now; History's empty state just
    # links there rather than hosting the button itself.
    assert 'href="/data"' in body


# --- /history/poll-now ------------------------------------------------------


class _FakeSolisClient:
    """SolisClient stand-in for /history/poll-now and /history/backfill tests.

    Quacks like the bits of `SolisClient` the `Poller` actually calls:
    `user_station_list` for discovery, `station_detail` per station,
    `station_month` for the daily-rollup backfill.
    """

    def __init__(
        self,
        *,
        stations: list[dict[str, Any]] | None = None,
        detail: dict[str, Any] | None = None,
        raise_on_list: BaseException | None = None,
        month_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._stations = stations or []
        self._detail = detail or {}
        self._raise_on_list = raise_on_list
        # Each station_month call returns the same canned rows by default.
        self._month_rows = month_rows or [
            {"dateStr": "2026-05-01", "energy": 10.0, "energyStr": "kWh"},
            {"dateStr": "2026-05-02", "energy": 11.0, "energyStr": "kWh"},
        ]
        self.month_calls: list[tuple[str, str]] = []
        self.day_calls: list[tuple[str, str]] = []

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

    async def station_month(
        self, *, station_id: Any = None, month: str = "", **_: Any
    ) -> list[dict[str, Any]]:
        self.month_calls.append((str(station_id), month))
        return list(self._month_rows)

    async def station_day(
        self, *, station_id: Any = None, time: str = "", **_: Any
    ) -> list[dict[str, Any]]:
        """Return two canned 5-minute samples for the requested date so
        the intraday backfill has something to upsert."""
        self.day_calls.append((str(station_id), time))
        # Build per-date timestamps so each call upserts distinct samples.
        try:
            d = datetime.fromisoformat(time).replace(tzinfo=timezone.utc)
        except ValueError:
            d = datetime(2026, 5, 1, tzinfo=timezone.utc)
        return [
            {
                "dataTimestamp": int(d.replace(hour=10).timestamp() * 1000),
                "psum": 1.0,
                "battery_soc": 50.0,
            },
            {
                "dataTimestamp": int(d.replace(hour=11).timestamp() * 1000),
                "psum": 2.0,
                "battery_soc": 60.0,
            },
        ]


async def test_history_poll_now_requires_auth(
    auth_client: httpx.AsyncClient,
) -> None:
    r = await auth_client.post("/history/poll-now", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_history_poll_now_writes_samples_and_daily_rollups(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """Poll button must populate both `station_samples` and `station_daily`.

    The original bug was that Poll only wrote samples; the History page's
    Month/Year/All views read `station_daily` and looked empty even after
    a successful poll. Now poll-now runs as a background task so the user
    sees progress while it's working — the test polls the status endpoint
    until completion, then asserts on the rows.
    """
    import asyncio
    import re

    from solisdash.app import app, get_solis_client

    fake = _FakeSolisClient(stations=[{"id": "S1"}, {"id": "S2"}])

    async def _override_solis() -> _FakeSolisClient:
        return fake

    app.dependency_overrides[get_solis_client] = _override_solis
    try:
        await _login(auth_client, clean_db)
        r = await auth_client.post("/history/poll-now")
        assert r.status_code == 200
        # Initial response is a polling fragment, NOT HX-Refresh.
        assert "hx-refresh" not in {k.lower() for k in r.headers}
        m = re.search(r"/history/backfill/status/(\S+?)\"", r.text)
        assert m is not None, "expected a backfill status URL in response"
        task_id = m.group(1)
        assert "<progress" in r.text  # progress bar present from tick 0

        # Wait for the background task to complete. The test fake has no
        # rate limit so this should be near-instantaneous, but we still
        # poll the same way real clients do.
        for _ in range(60):
            await asyncio.sleep(0.05)
            poll = await auth_client.get(f"/history/backfill/status/{task_id}")
            if poll.headers.get("hx-refresh") == "true":
                break
        else:
            raise AssertionError("poll-now never completed within 3s")

        # Samples written: 2 from stationDetail (one per station) + 2 per
        # station from the fake's stationDay (today's intraday backfill)
        # = 6 total.
        assert await clean_db["station_samples"].count_documents({}) == 6
        assert await clean_db["stations"].count_documents({}) == 2
        # Daily rollups written for the Month / Year / All charts. Two
        # canned rows per (station, month) call; upserts dedupe by
        # (station_id, date), so for two stations we get the same two
        # canned dates * two stations = 4 docs.
        assert await clean_db["station_daily"].count_documents({}) == 4
        # And each station saw at least one station_month call.
        called_stations = {sid for sid, _ in fake.month_calls}
        assert called_stations == {"S1", "S2"}
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


# --- /history/backfill ----------------------------------------------------


async def test_history_backfill_requires_auth(auth_client: httpx.AsyncClient) -> None:
    r = await auth_client.post(
        "/history/backfill",
        data={"start": "2025-01-01", "end": "2025-12-31"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


async def test_history_backfill_kicks_off_task_and_polls_progress(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    """POST /history/backfill returns a polling fragment, the background
    task runs, and a subsequent GET on the status endpoint shows progress
    and finally HX-Refresh on completion."""
    import asyncio
    import re

    from solisdash.app import app, get_solis_client

    fake = _FakeSolisClient(stations=[{"id": "S1"}])

    async def _override_solis() -> _FakeSolisClient:
        return fake

    app.dependency_overrides[get_solis_client] = _override_solis
    try:
        await _login(auth_client, clean_db)
        r = await auth_client.post(
            "/history/backfill",
            data={"start": "2026-04-01", "end": "2026-05-31"},
        )
        assert r.status_code == 200
        # Initial response is a polling fragment, NOT HX-Refresh — that
        # only comes once the background task is done.
        assert "hx-refresh" not in {k.lower() for k in r.headers}
        m = re.search(r"/history/backfill/status/(\S+?)\"", r.text)
        assert m is not None, "expected a backfill status URL in response"
        task_id = m.group(1)
        assert "<progress" in r.text  # progress bar rendered
        assert "Downloading" in r.text

        # Backfill now also pulls intraday for every day in the range —
        # 2 months * 1 station + 61 days * 1 station ~= 63 calls. Each
        # is instant under the no-rate-limit fixture but every upsert
        # is a real Mongo round-trip. Under -n auto the workers contend
        # for connection-pool slots, so we leave a generous timeout.
        deadline = 30.0
        elapsed = 0.0
        while elapsed < deadline:
            await asyncio.sleep(0.1)
            elapsed += 0.1
            poll = await auth_client.get(f"/history/backfill/status/{task_id}")
            if poll.headers.get("hx-refresh") == "true":
                break
        else:
            raise AssertionError(f"backfill never completed within {deadline}s")
        assert "Backfilled" in poll.text
        assert [m for _, m in fake.month_calls] == ["2026-04", "2026-05"]
        # 2 months times 2 canned rows = 4 inserts, de-duped to 2 unique dates.
        assert await clean_db["station_daily"].count_documents({}) == 2
    finally:
        app.dependency_overrides.pop(get_solis_client, None)


async def test_history_backfill_status_endpoint_404s_unknown_task(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.get("/history/backfill/status/does-not-exist")
    assert r.status_code == 200  # HTMX fragments are always 200
    assert "Unknown backfill task" in r.text


async def test_history_backfill_rejects_inverted_range(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.post(
        "/history/backfill",
        data={"start": "2026-12-31", "end": "2026-01-01"},
    )
    assert r.status_code == 200
    assert "must not be before start" in r.text.lower()


async def test_history_backfill_rejects_bad_date(
    auth_client: httpx.AsyncClient, clean_db: AsyncDatabase[dict[str, Any]]
) -> None:
    await _login(auth_client, clean_db)
    r = await auth_client.post(
        "/history/backfill",
        data={"start": "not-a-date", "end": "2026-01-01"},
    )
    assert r.status_code == 200
    assert "bad date" in r.text.lower()


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
