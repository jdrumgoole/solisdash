from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.history import (
    HistoryService,
    Series,
    month_day_range,
    parse_month,
)

# --- parse_month / month_day_range ----------------------------------------


def test_parse_month_ok() -> None:
    assert parse_month("2026-05") == (2026, 5)
    assert parse_month("2000-12") == (2000, 12)


@pytest.mark.parametrize("bad", ["2026", "2026-13", "2026-00", "xx-05", ""])
def test_parse_month_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_month(bad)


def test_month_day_range_covers_full_month() -> None:
    assert month_day_range(2026, 2) == ("2026-02-01", "2026-02-28")
    assert month_day_range(2024, 2) == ("2024-02-01", "2024-02-29")
    assert month_day_range(2026, 12) == ("2026-12-01", "2026-12-31")


# --- HistoryService against a real test DB --------------------------------


async def test_list_stations_returns_alpha_sorted(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await clean_db["stations"].insert_many(
        [
            {"id": "B", "stationName": "Beta"},
            {"id": "A", "stationName": "Alpha"},
        ]
    )
    service = HistoryService(clean_db)
    stations = await service.list_stations()
    assert [s["name"] for s in stations] == ["Alpha", "Beta"]
    assert [s["id"] for s in stations] == ["A", "B"]


async def test_list_stations_empty_when_nothing_polled(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    service = HistoryService(clean_db)
    assert await service.list_stations() == []


async def test_day_series_returns_5min_samples_for_that_day(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    base = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc)
    await clean_db["station_samples"].insert_many(
        [
            {"station_id": "S1", "ts": base, "psum": 1.0, "power_unit": "kW"},
            {
                "station_id": "S1",
                "ts": base.replace(hour=14, minute=30),
                "psum": 2.0,
                "power_unit": "kW",
            },
            # Same day, different station — must be excluded
            {"station_id": "S2", "ts": base, "psum": 99.0},
            # Different day — must be excluded
            {
                "station_id": "S1",
                "ts": datetime(2026, 5, 14, 1, 0, tzinfo=timezone.utc),
                "psum": 5.0,
            },
        ]
    )
    service = HistoryService(clean_db)
    series = await service.day_series("S1", date(2026, 5, 13))
    assert isinstance(series, Series)
    assert series.unit == "kW"
    assert [p.v for p in series.points] == [1.0, 2.0]
    # Sorted ascending by ts
    assert series.points[0].t < series.points[1].t  # type: ignore[operator]


async def test_day_series_falls_back_to_power_when_no_psum(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await clean_db["station_samples"].insert_one(
        {
            "station_id": "S1",
            "ts": datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
            "power": 3.3,
        }
    )
    service = HistoryService(clean_db)
    series = await service.day_series("S1", date(2026, 5, 13))
    assert [p.v for p in series.points] == [3.3]


async def test_month_daily_only_returns_rows_in_that_month(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await clean_db["station_daily"].insert_many(
        [
            {"station_id": "S1", "date": "2026-04-30", "energy": 9.0, "energy_unit": "kWh"},
            {"station_id": "S1", "date": "2026-05-01", "energy": 10.0, "energy_unit": "kWh"},
            {"station_id": "S1", "date": "2026-05-31", "energy": 11.0, "energy_unit": "kWh"},
            {"station_id": "S1", "date": "2026-06-01", "energy": 12.0, "energy_unit": "kWh"},
            # Different station — excluded
            {"station_id": "S2", "date": "2026-05-15", "energy": 99.0},
        ]
    )
    service = HistoryService(clean_db)
    series = await service.month_daily("S1", "2026-05")
    assert [(p.t, p.v) for p in series.points] == [
        ("2026-05-01", 10.0),
        ("2026-05-31", 11.0),
    ]
    assert series.unit == "kWh"


async def test_year_monthly_aggregates_station_daily_by_month(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    rows: list[dict[str, Any]] = []
    for day in range(1, 6):
        rows.append({"station_id": "S1", "date": f"2026-01-{day:02d}", "energy": 2.0})
    for day in range(1, 4):
        rows.append({"station_id": "S1", "date": f"2026-03-{day:02d}", "energy": 3.0})
    # Different year — excluded
    rows.append({"station_id": "S1", "date": "2025-01-01", "energy": 999.0})
    # Different station — excluded
    rows.append({"station_id": "S2", "date": "2026-01-01", "energy": 999.0})
    await clean_db["station_daily"].insert_many(rows)

    service = HistoryService(clean_db)
    series = await service.year_monthly("S1", 2026)
    assert [(p.t, p.v) for p in series.points] == [
        ("2026-01", 10.0),
        ("2026-03", 9.0),
    ]


async def test_all_time_aggregates_station_daily_by_year(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await clean_db["station_daily"].insert_many(
        [
            {"station_id": "S1", "date": "2024-05-01", "energy": 1.0},
            {"station_id": "S1", "date": "2024-12-31", "energy": 2.0},
            {"station_id": "S1", "date": "2025-01-01", "energy": 5.0},
            {"station_id": "S1", "date": "2025-06-15", "energy": 7.0},
            {"station_id": "S2", "date": "2024-01-01", "energy": 999.0},
        ]
    )
    service = HistoryService(clean_db)
    series = await service.all_time("S1")
    assert [(p.t, p.v) for p in series.points] == [
        ("2024", 3.0),
        ("2025", 12.0),
    ]


async def test_series_to_json_shape() -> None:
    from solisdash.history import Point

    s = Series(label="Power", unit="kW", points=[Point(t=1000, v=4.2), Point(t=2000, v=None)])
    assert s.to_json() == {
        "label": "Power",
        "unit": "kW",
        "points": [{"t": 1000, "v": 4.2}, {"t": 2000, "v": None}],
    }
