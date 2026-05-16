"""History chart queries — Mongo only, no SolisCloud calls on this path."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

# Metric identifiers used by HTTP endpoints (`?metric=`) and the History
# page's tab bar. Each metric is valid for a subset of views; the registry
# below is the single source of truth, consumed by both the service and the
# JSON endpoints.
METRIC_POWER = "power"
METRIC_ENERGY = "energy"
METRIC_BATTERY = "battery"
METRIC_MONEY = "money"
METRIC_ALARMS = "alarms"

VIEW_DAY = "day"
VIEW_MONTH = "month"
VIEW_YEAR = "year"
VIEW_ALL = "all"

# (metric, view) → True if the combo is supported. The History page disables
# unsupported view options when a metric tab is selected.
METRIC_SUPPORTS: dict[str, set[str]] = {
    METRIC_POWER: {VIEW_DAY},
    METRIC_ENERGY: {VIEW_DAY, VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_BATTERY: {VIEW_DAY},
    METRIC_MONEY: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_ALARMS: {VIEW_DAY},
}


def metric_supports(metric: str, view: str) -> bool:
    return view in METRIC_SUPPORTS.get(metric, set())


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


# --- per-sample (Day view) field projection ---------------------------------


@dataclass(frozen=True)
class _SampleField:
    """Description of how a Day-view metric pulls from `station_samples`."""

    field: str
    label: str
    default_unit: str
    unit_field: str | None = None
    fallback_field: str | None = None  # try this if `field` is missing


_DAY_METRIC_FIELDS: dict[str, _SampleField] = {
    METRIC_POWER: _SampleField(
        field="psum",
        label="Power",
        default_unit="kW",
        unit_field="power_unit",
        fallback_field="power",
    ),
    METRIC_ENERGY: _SampleField(
        field="day_energy",
        label="Day energy",
        default_unit="kWh",
        unit_field="day_energy_unit",
    ),
    METRIC_BATTERY: _SampleField(
        field="battery_soc",
        label="Battery SOC",
        default_unit="%",
    ),
    METRIC_ALARMS: _SampleField(
        field="alarm_count",
        label="Open alarms",
        default_unit="",
    ),
}


# --- per-day-rollup (Month/Year/All views) field projection -----------------


@dataclass(frozen=True)
class _DailyField:
    """Description of how a non-Day-view metric pulls from `station_daily`."""

    field: str
    monthly_label: str  # shown for Month view (one bar per day)
    yearly_label: str  # shown for Year view (one bar per month)
    alltime_label: str  # shown for All-time view (one bar per year)
    default_unit: str
    unit_field: str | None = None


_DAILY_METRIC_FIELDS: dict[str, _DailyField] = {
    METRIC_ENERGY: _DailyField(
        field="energy",
        monthly_label="Daily energy",
        yearly_label="Monthly energy",
        alltime_label="Annual energy",
        default_unit="kWh",
        unit_field="energy_unit",
    ),
    METRIC_MONEY: _DailyField(
        field="money",
        monthly_label="Daily revenue",
        yearly_label="Monthly revenue",
        alltime_label="Annual revenue",
        default_unit="",
        unit_field="money_unit",
    ),
}


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

    # --- Day view -----------------------------------------------------------

    async def day_series(
        self, station_id: str, d: date, *, metric: str = METRIC_POWER
    ) -> Series:
        """One sample-resolution series over the given date.

        Backward-compatible with the original signature: with no `metric`
        argument, returns the Power series.
        """
        spec = _DAY_METRIC_FIELDS.get(metric)
        if spec is None:
            raise ValueError(f"unsupported day metric: {metric!r}")
        start, end = _day_bounds(d)
        cursor = self._db["station_samples"].find(
            {"station_id": station_id, "ts": {"$gte": start, "$lte": end}},
            sort=[("ts", 1)],
        )
        points: list[Point] = []
        unit = spec.default_unit
        async for doc in cursor:
            ts: Any = doc.get("ts")
            ts_ms = int(ts.timestamp() * 1000) if isinstance(ts, datetime) else 0
            v = doc.get(spec.field)
            if v is None and spec.fallback_field is not None:
                v = doc.get(spec.fallback_field)
            points.append(Point(t=ts_ms, v=float(v) if v is not None else None))
            if spec.unit_field and doc.get(spec.unit_field):
                unit = str(doc[spec.unit_field])
        return Series(label=spec.label, unit=unit, points=points)

    # --- Month view ---------------------------------------------------------

    async def month_daily(
        self, station_id: str, month: str, *, metric: str = METRIC_ENERGY
    ) -> Series:
        """One bar per day of the month for `metric`."""
        spec = _DAILY_METRIC_FIELDS.get(metric)
        if spec is None:
            raise ValueError(f"unsupported month metric: {metric!r}")
        year, m = parse_month(month)
        start, end = month_day_range(year, m)
        cursor = self._db["station_daily"].find(
            {"station_id": station_id, "date": {"$gte": start, "$lte": end}},
            sort=[("date", 1)],
        )
        points: list[Point] = []
        unit = spec.default_unit
        async for doc in cursor:
            v = doc.get(spec.field)
            points.append(
                Point(t=str(doc.get("date")), v=float(v) if v is not None else None)
            )
            if spec.unit_field and doc.get(spec.unit_field):
                unit = str(doc[spec.unit_field])
        return Series(label=spec.monthly_label, unit=unit, points=points)

    # --- Year view ----------------------------------------------------------

    async def year_monthly(
        self, station_id: str, year: int, *, metric: str = METRIC_ENERGY
    ) -> Series:
        spec = _DAILY_METRIC_FIELDS.get(metric)
        if spec is None:
            raise ValueError(f"unsupported year metric: {metric!r}")
        start, end = f"{year:04d}-01-01", f"{year:04d}-12-31"
        pipeline: list[dict[str, Any]] = [
            {"$match": {"station_id": station_id, "date": {"$gte": start, "$lte": end}}},
            {
                "$group": {
                    "_id": {"$substr": ["$date", 0, 7]},  # YYYY-MM
                    "v": {"$sum": f"${spec.field}"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        unit = await self._first_unit("station_daily", spec, station_id) or spec.default_unit
        points: list[Point] = []
        cursor = await self._db["station_daily"].aggregate(pipeline)
        async for row in cursor:
            points.append(Point(t=str(row["_id"]), v=float(row.get("v") or 0.0)))
        return Series(label=spec.yearly_label, unit=unit, points=points)

    # --- All-time view ------------------------------------------------------

    async def all_time(
        self, station_id: str, *, metric: str = METRIC_ENERGY
    ) -> Series:
        spec = _DAILY_METRIC_FIELDS.get(metric)
        if spec is None:
            raise ValueError(f"unsupported all-time metric: {metric!r}")
        pipeline: list[dict[str, Any]] = [
            {"$match": {"station_id": station_id}},
            {
                "$group": {
                    "_id": {"$substr": ["$date", 0, 4]},  # YYYY
                    "v": {"$sum": f"${spec.field}"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        unit = await self._first_unit("station_daily", spec, station_id) or spec.default_unit
        points: list[Point] = []
        cursor = await self._db["station_daily"].aggregate(pipeline)
        async for row in cursor:
            points.append(Point(t=str(row["_id"]), v=float(row.get("v") or 0.0)))
        return Series(label=spec.alltime_label, unit=unit, points=points)

    # --- helpers ------------------------------------------------------------

    async def _first_unit(
        self, collection: str, spec: _DailyField, station_id: str
    ) -> str | None:
        """Pick a unit string from the most recent `station_daily` row.

        Year/All views aggregate across rows, so we lose the per-row unit
        field in the pipeline. Look one up once for the label.
        """
        if not spec.unit_field:
            return None
        doc = await self._db[collection].find_one(
            {"station_id": station_id, spec.unit_field: {"$ne": None}},
            sort=[("date", -1)],
            projection={spec.unit_field: 1},
        )
        if doc is None:
            return None
        unit = doc.get(spec.unit_field)
        return str(unit) if unit else None

    # --- live-tile sparklines ----------------------------------------------

    async def recent_samples(
        self, station_id: str, *, hours: int = 24, fields: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """Return up to `hours` of recent `station_samples` rows, oldest first.

        Powers the sparklines under the home-page live tiles. Keep the
        projection narrow so we don't ship 100KB of unused detail to the
        template just to draw four 60-pixel lines.
        """
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        projection: dict[str, int] = {"_id": 0, "ts": 1}
        for f in fields:
            projection[f] = 1
        cursor = self._db["station_samples"].find(
            {"station_id": station_id, "ts": {"$gte": cutoff}},
            sort=[("ts", 1)],
            projection=projection,
        )
        return [doc async for doc in cursor]
