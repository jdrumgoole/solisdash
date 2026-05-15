"""History chart queries — Mongo only, no SolisCloud calls on this path."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase


@dataclass(frozen=True)
class Point:
    """One x/y point in a chart series. `t` is JSON-friendly (ms or string)."""

    t: str | int
    v: float | None


@dataclass(frozen=True)
class Series:
    label: str
    unit: str
    points: list[Point]

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "unit": self.unit,
            "points": [{"t": p.t, "v": p.v} for p in self.points],
        }


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min, tzinfo=timezone.utc)
    end = datetime.combine(d, time.max, tzinfo=timezone.utc)
    return start, end


def parse_month(month: str) -> tuple[int, int]:
    """`YYYY-MM` → (year, month). Raises `ValueError` on bad input."""
    parts = month.split("-")
    if len(parts) != 2:
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    y, m = int(parts[0]), int(parts[1])
    if not 1 <= m <= 12:
        raise ValueError(f"month out of range: {month!r}")
    return y, m


def month_day_range(year: int, month: int) -> tuple[str, str]:
    """First and last calendar dates of `year-month`, as ISO strings."""
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


class HistoryService:
    """Read-only projections over `station_samples` and `station_daily`."""

    def __init__(self, db: AsyncDatabase[dict[str, Any]]) -> None:
        self._db = db

    async def list_stations(self) -> list[dict[str, Any]]:
        """Stations the poller has seen. Empty if nothing's polled yet."""
        cursor = self._db["stations"].find({}, sort=[("stationName", 1)])
        return [
            {"id": str(doc.get("id")), "name": str(doc.get("stationName") or "")}
            async for doc in cursor
        ]

    async def day_series(self, station_id: str, d: date) -> Series:
        start, end = _day_bounds(d)
        cursor = (
            self._db["station_samples"]
            .find(
                {"station_id": station_id, "ts": {"$gte": start, "$lte": end}},
                sort=[("ts", 1)],
            )
        )
        points: list[Point] = []
        unit = "kW"
        async for doc in cursor:
            ts: Any = doc.get("ts")
            ts_ms = int(ts.timestamp() * 1000) if isinstance(ts, datetime) else 0
            v = doc.get("psum")
            if v is None:
                v = doc.get("power")
            points.append(Point(t=ts_ms, v=float(v) if v is not None else None))
            if doc.get("power_unit"):
                unit = str(doc["power_unit"])
        return Series(label="Power", unit=unit, points=points)

    async def month_daily(self, station_id: str, month: str) -> Series:
        year, m = parse_month(month)
        start, end = month_day_range(year, m)
        cursor = (
            self._db["station_daily"]
            .find(
                {"station_id": station_id, "date": {"$gte": start, "$lte": end}},
                sort=[("date", 1)],
            )
        )
        points: list[Point] = []
        unit = "kWh"
        async for doc in cursor:
            v = doc.get("energy")
            points.append(
                Point(t=str(doc.get("date")), v=float(v) if v is not None else None)
            )
            if doc.get("energy_unit"):
                unit = str(doc["energy_unit"])
        return Series(label="Daily energy", unit=unit, points=points)

    async def year_monthly(self, station_id: str, year: int) -> Series:
        start, end = f"{year:04d}-01-01", f"{year:04d}-12-31"
        pipeline: list[dict[str, Any]] = [
            {"$match": {"station_id": station_id, "date": {"$gte": start, "$lte": end}}},
            {
                "$group": {
                    "_id": {"$substr": ["$date", 0, 7]},  # YYYY-MM
                    "energy": {"$sum": "$energy"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        points: list[Point] = []
        cursor = await self._db["station_daily"].aggregate(pipeline)
        async for row in cursor:
            points.append(Point(t=str(row["_id"]), v=float(row.get("energy") or 0.0)))
        return Series(label="Monthly energy", unit="kWh", points=points)

    async def all_time(self, station_id: str) -> Series:
        pipeline: list[dict[str, Any]] = [
            {"$match": {"station_id": station_id}},
            {
                "$group": {
                    "_id": {"$substr": ["$date", 0, 4]},  # YYYY
                    "energy": {"$sum": "$energy"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        points: list[Point] = []
        cursor = await self._db["station_daily"].aggregate(pipeline)
        async for row in cursor:
            points.append(Point(t=str(row["_id"]), v=float(row.get("energy") or 0.0)))
        return Series(label="Annual energy", unit="kWh", points=points)
