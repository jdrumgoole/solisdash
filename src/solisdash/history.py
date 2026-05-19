"""History chart queries — Mongo only, no SolisCloud calls on this path."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
METRIC_BATTERY_POWER = "battery_power"
METRIC_BATTERY_CHARGE = "battery_charge"
METRIC_BATTERY_DISCHARGE = "battery_discharge"
METRIC_CONSUMPTION = "consumption"
METRIC_IMPORT = "import_energy"
METRIC_EXPORT = "export_energy"
METRIC_NET = "net"
METRIC_CASHFLOW = "cashflow"
METRIC_TOTAL_OUTPUT = "total_output"

VIEW_DAY = "day"
VIEW_MONTH = "month"
VIEW_YEAR = "year"
VIEW_ALL = "all"

# Resolution identifiers used by the History page's Resolution dropdown
# and the `?resolution=` query parameter. "auto" means the server picks
# based on (metric, span); the others force a specific bucketing.
RESOLUTION_AUTO = "auto"
RESOLUTION_SAMPLES = "samples"   # raw 5-min station_samples rows
RESOLUTION_DAILY = "daily"        # one bucket per day
RESOLUTION_MONTHLY = "monthly"    # one bucket per YYYY-MM
RESOLUTION_YEARLY = "yearly"      # one bucket per YYYY
RESOLUTIONS = (
    RESOLUTION_AUTO,
    RESOLUTION_SAMPLES,
    RESOLUTION_DAILY,
    RESOLUTION_MONTHLY,
    RESOLUTION_YEARLY,
)

# (metric, view) → True if the combo is supported. The History page disables
# unsupported view options when a metric tab is selected.
METRIC_SUPPORTS: dict[str, set[str]] = {
    METRIC_POWER: {VIEW_DAY},
    METRIC_ENERGY: {VIEW_DAY, VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_BATTERY: {VIEW_DAY},
    METRIC_MONEY: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_ALARMS: {VIEW_DAY},
    METRIC_BATTERY_POWER: {VIEW_DAY},
    METRIC_BATTERY_CHARGE: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_BATTERY_DISCHARGE: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_CONSUMPTION: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_IMPORT: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_EXPORT: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_NET: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_CASHFLOW: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
    METRIC_TOTAL_OUTPUT: {VIEW_MONTH, VIEW_YEAR, VIEW_ALL},
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
    METRIC_BATTERY_POWER: _SampleField(
        field="battery_power",
        label="Battery power",
        default_unit="kW",
        unit_field="battery_power_unit",
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
    METRIC_BATTERY_CHARGE: _DailyField(
        field="battery_charge",
        monthly_label="Daily battery charge",
        yearly_label="Monthly battery charge",
        alltime_label="Annual battery charge",
        default_unit="kWh",
        unit_field="battery_charge_unit",
    ),
    METRIC_BATTERY_DISCHARGE: _DailyField(
        field="battery_discharge",
        monthly_label="Daily battery discharge",
        yearly_label="Monthly battery discharge",
        alltime_label="Annual battery discharge",
        default_unit="kWh",
        unit_field="battery_discharge_unit",
    ),
    METRIC_CONSUMPTION: _DailyField(
        field="consumption",
        monthly_label="Daily consumption",
        yearly_label="Monthly consumption",
        alltime_label="Annual consumption",
        default_unit="kWh",
        unit_field="consumption_unit",
    ),
    METRIC_IMPORT: _DailyField(
        field="import_energy",
        monthly_label="Daily grid import",
        yearly_label="Monthly grid import",
        alltime_label="Annual grid import",
        default_unit="kWh",
        unit_field="import_energy_unit",
    ),
    METRIC_EXPORT: _DailyField(
        field="export_energy",
        monthly_label="Daily grid export",
        yearly_label="Monthly grid export",
        alltime_label="Annual grid export",
        default_unit="kWh",
        unit_field="export_energy_unit",
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

    # --- arbitrary-range queries ------------------------------------------

    async def _range_samples_aggregated(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        metric: str,
        bucket: str,
    ) -> Series:
        """Mean of `spec.field` per bucket (daily / monthly / yearly).

        Lets sample-only metrics (Power, Battery SOC, …) share the same
        x-axis bucketing as the daily-rollup metrics across wide ranges,
        so switching tabs doesn't jolt the chart shape."""
        spec = _DAY_METRIC_FIELDS[metric]
        date_fmt = {
            "daily": "%Y-%m-%d",
            "monthly": "%Y-%m",
            "yearly": "%Y",
        }[bucket]
        start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "station_id": station_id,
                    "ts": {"$gte": start_dt, "$lte": end_dt},
                    spec.field: {"$ne": None},
                }
            },
            {
                "$group": {
                    "_id": {"$dateToString": {"format": date_fmt, "date": "$ts"}},
                    "v": {"$avg": f"${spec.field}"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        points: list[Point] = []
        cursor = await self._db["station_samples"].aggregate(pipeline)
        async for row in cursor:
            v = row.get("v")
            points.append(
                Point(t=str(row["_id"]), v=float(v) if v is not None else None)
            )
        return Series(label=spec.label, unit=spec.default_unit, points=points)

    async def _range_samples(
        self, station_id: str, start_date: date, end_date: date, *, metric: str
    ) -> Series:
        spec = _DAY_METRIC_FIELDS[metric]
        start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        end_dt = datetime.combine(end_date, time.max, tzinfo=timezone.utc)
        cursor = self._db["station_samples"].find(
            {"station_id": station_id, "ts": {"$gte": start_dt, "$lte": end_dt}},
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

    async def _range_daily(
        self, station_id: str, start_date: date, end_date: date, *, metric: str
    ) -> Series:
        spec = _DAILY_METRIC_FIELDS[metric]
        cursor = self._db["station_daily"].find(
            {
                "station_id": station_id,
                "date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()},
            },
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

    async def _range_aggregate(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        metric: str,
        bucket_chars: int,
        label_attr: str,
    ) -> Series:
        spec = _DAILY_METRIC_FIELDS[metric]
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "station_id": station_id,
                    "date": {
                        "$gte": start_date.isoformat(),
                        "$lte": end_date.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": {"$substr": ["$date", 0, bucket_chars]},
                    "v": {"$sum": f"${spec.field}"},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        unit = (
            await self._first_unit("station_daily", spec, station_id)
            or spec.default_unit
        )
        points: list[Point] = []
        cursor = await self._db["station_daily"].aggregate(pipeline)
        async for row in cursor:
            points.append(Point(t=str(row["_id"]), v=float(row.get("v") or 0.0)))
        return Series(label=getattr(spec, label_attr), unit=unit, points=points)

    # --- net export = export_energy - import_energy --------------------

    async def _range_net_daily(
        self, station_id: str, start_date: date, end_date: date
    ) -> Series:
        """Per-day (export - import) in kWh. Positive = the house sold back
        more than it pulled in that day, negative = net buyer."""
        cursor = self._db["station_daily"].find(
            {
                "station_id": station_id,
                "date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()},
            },
            sort=[("date", 1)],
        )
        points: list[Point] = []
        async for doc in cursor:
            ex = doc.get("export_energy")
            im = doc.get("import_energy")
            if ex is None and im is None:
                v: float | None = None
            else:
                v = float(ex or 0.0) - float(im or 0.0)
            points.append(Point(t=str(doc.get("date")), v=v))
        return Series(label="Daily net (export - import)", unit="kWh", points=points)

    async def _range_net_aggregate(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        bucket_chars: int,
        label: str,
    ) -> Series:
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "station_id": station_id,
                    "date": {
                        "$gte": start_date.isoformat(),
                        "$lte": end_date.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": {"$substr": ["$date", 0, bucket_chars]},
                    "v": {
                        "$sum": {
                            "$subtract": [
                                {"$ifNull": ["$export_energy", 0]},
                                {"$ifNull": ["$import_energy", 0]},
                            ]
                        }
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]
        points: list[Point] = []
        cursor = await self._db["station_daily"].aggregate(pipeline)
        async for row in cursor:
            points.append(Point(t=str(row["_id"]), v=float(row.get("v") or 0.0)))
        return Series(label=label, unit="kWh", points=points)

    async def _range_cashflow(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        feed_in_tariff: float,
        import_tariff: float,
        currency: str,
        bucket_chars: int,
        label: str,
    ) -> Series:
        """`export * feed_in - import * import_tariff` summed per bucket."""
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "station_id": station_id,
                    "date": {
                        "$gte": start_date.isoformat(),
                        "$lte": end_date.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": {"$substr": ["$date", 0, bucket_chars]},
                    "export_kwh": {
                        "$sum": {"$ifNull": ["$export_energy", 0]}
                    },
                    "import_kwh": {
                        "$sum": {"$ifNull": ["$import_energy", 0]}
                    },
                }
            },
            {"$sort": {"_id": 1}},
        ]
        points: list[Point] = []
        cursor = await self._db["station_daily"].aggregate(pipeline)
        async for row in cursor:
            ex = float(row.get("export_kwh") or 0.0)
            im = float(row.get("import_kwh") or 0.0)
            points.append(
                Point(t=str(row["_id"]), v=ex * feed_in_tariff - im * import_tariff)
            )
        return Series(label=label, unit=currency, points=points)

    async def _range_cashflow_daily(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        feed_in_tariff: float,
        import_tariff: float,
        currency: str,
    ) -> Series:
        """Per-day cashflow row by row, label `YYYY-MM-DD`."""
        cursor = self._db["station_daily"].find(
            {
                "station_id": station_id,
                "date": {"$gte": start_date.isoformat(), "$lte": end_date.isoformat()},
            },
            sort=[("date", 1)],
        )
        points: list[Point] = []
        async for doc in cursor:
            ex = float(doc.get("export_energy") or 0.0)
            im = float(doc.get("import_energy") or 0.0)
            points.append(
                Point(t=str(doc.get("date")), v=ex * feed_in_tariff - im * import_tariff)
            )
        return Series(label="Daily cashflow", unit=currency, points=points)

    async def _explicit_resolution(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        metric: str,
        resolution: str,
        feed_in_tariff: float,
        import_tariff: float,
        currency: str,
    ) -> tuple[Series, str, date, date]:
        """Honour a user-selected resolution. Raises ValueError for combos
        that don't make sense (e.g. Money at sample resolution — money
        is only ever stored per-day)."""
        sample_metrics = {
            METRIC_POWER,
            METRIC_BATTERY,
            METRIC_ALARMS,
            METRIC_BATTERY_POWER,
        }

        if resolution == RESOLUTION_SAMPLES:
            if metric not in sample_metrics and metric != METRIC_ENERGY:
                raise ValueError(
                    f"metric {metric!r} has no sample-resolution data; "
                    "pick Daily / Monthly / Yearly"
                )
            spec = _DAY_METRIC_FIELDS.get(metric)
            if spec is None:
                raise ValueError(f"no sample-shape for metric {metric!r}")
            series = await self._range_samples(
                station_id, start_date, end_date, metric=metric
            )
            return series, "5-min samples", start_date, end_date

        if resolution == RESOLUTION_DAILY:
            if metric in sample_metrics:
                series = await self._range_samples_aggregated(
                    station_id, start_date, end_date,
                    metric=metric, bucket="daily",
                )
                return series, "daily averages", start_date, end_date
            if metric == METRIC_TOTAL_OUTPUT:
                return await self._cumulative(
                    station_id, start_date, end_date, resolution=resolution,
                    feed_in_tariff=feed_in_tariff,
                    import_tariff=import_tariff,
                    currency=currency,
                )
            if metric == METRIC_NET:
                series = await self._range_net_daily(
                    station_id, start_date, end_date
                )
                return series, "daily totals", start_date, end_date
            if metric == METRIC_CASHFLOW:
                series = await self._range_cashflow_daily(
                    station_id, start_date, end_date,
                    feed_in_tariff=feed_in_tariff,
                    import_tariff=import_tariff,
                    currency=currency,
                )
                return series, "daily totals", start_date, end_date
            if metric in _DAILY_METRIC_FIELDS:
                series = await self._range_daily(
                    station_id, start_date, end_date, metric=metric
                )
                return series, "daily totals", start_date, end_date

        if resolution in (RESOLUTION_MONTHLY, RESOLUTION_YEARLY):
            bucket_chars = 7 if resolution == RESOLUTION_MONTHLY else 4
            label_attr = (
                "yearly_label"
                if resolution == RESOLUTION_MONTHLY
                else "alltime_label"
            )
            res_label = (
                "monthly totals"
                if resolution == RESOLUTION_MONTHLY
                else "yearly totals"
            )
            if metric in sample_metrics:
                bucket = (
                    "monthly" if resolution == RESOLUTION_MONTHLY else "yearly"
                )
                series = await self._range_samples_aggregated(
                    station_id, start_date, end_date,
                    metric=metric, bucket=bucket,
                )
                avg_label = (
                    "monthly averages"
                    if resolution == RESOLUTION_MONTHLY
                    else "yearly averages"
                )
                return series, avg_label, start_date, end_date
            if metric == METRIC_TOTAL_OUTPUT:
                return await self._cumulative(
                    station_id, start_date, end_date, resolution=resolution,
                    feed_in_tariff=feed_in_tariff,
                    import_tariff=import_tariff,
                    currency=currency,
                )
            if metric == METRIC_NET:
                lbl = (
                    "Monthly net (export - import)"
                    if resolution == RESOLUTION_MONTHLY
                    else "Annual net (export - import)"
                )
                series = await self._range_net_aggregate(
                    station_id, start_date, end_date,
                    bucket_chars=bucket_chars,
                    label=lbl,
                )
                return series, res_label, start_date, end_date
            if metric == METRIC_CASHFLOW:
                lbl = (
                    "Monthly cashflow"
                    if resolution == RESOLUTION_MONTHLY
                    else "Annual cashflow"
                )
                series = await self._range_cashflow(
                    station_id, start_date, end_date,
                    feed_in_tariff=feed_in_tariff,
                    import_tariff=import_tariff,
                    currency=currency,
                    bucket_chars=bucket_chars,
                    label=lbl,
                )
                return series, res_label, start_date, end_date
            if metric in _DAILY_METRIC_FIELDS:
                series = await self._range_aggregate(
                    station_id, start_date, end_date,
                    metric=metric,
                    bucket_chars=bucket_chars,
                    label_attr=label_attr,
                )
                return series, res_label, start_date, end_date

        raise ValueError(
            f"can't compute metric {metric!r} at resolution {resolution!r}"
        )

    async def _cumulative(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        resolution: str,
        feed_in_tariff: float,
        import_tariff: float,
        currency: str,
    ) -> tuple[Series, str, date, date]:
        """Cumulative version of the Energy series at the chosen resolution."""
        base, res_label, eff_start, eff_end = await self.auto_range(
            station_id, start_date, end_date,
            metric=METRIC_ENERGY,
            requested_resolution=resolution,
            feed_in_tariff=feed_in_tariff,
            import_tariff=import_tariff,
            currency=currency,
        )
        running = 0.0
        cumulative: list[Point] = []
        for p in base.points:
            if p.v is not None:
                running += p.v
            cumulative.append(Point(t=p.t, v=running))
        return (
            Series(
                label="Cumulative production",
                unit=base.unit,
                points=cumulative,
            ),
            res_label,
            eff_start,
            eff_end,
        )

    async def auto_range(
        self,
        station_id: str,
        start_date: date,
        end_date: date,
        *,
        metric: str,
        requested_resolution: str = RESOLUTION_AUTO,
        feed_in_tariff: float = 0.0,
        import_tariff: float = 0.0,
        currency: str = "EUR",
    ) -> tuple[Series, str, date, date]:
        """Return a series for [start_date, end_date].

        Returns `(series, resolution_label, effective_start, effective_end)`.
        `requested_resolution="auto"` lets the function pick based on
        (metric, span); any other value forces that bucketing — see
        `RESOLUTIONS` and `resolution_supported()`. Raises ValueError
        for unsupported (metric, resolution) combos.
        """
        if end_date < start_date:
            start_date, end_date = end_date, start_date
        span_days = (end_date - start_date).days + 1

        # Explicit-resolution short-circuit. Falls through to the
        # auto-by-span logic only when caller asked for AUTO.
        if requested_resolution != RESOLUTION_AUTO:
            return await self._explicit_resolution(
                station_id,
                start_date,
                end_date,
                metric=metric,
                resolution=requested_resolution,
                feed_in_tariff=feed_in_tariff,
                import_tariff=import_tariff,
                currency=currency,
            )

        if metric in (
            METRIC_POWER,
            METRIC_BATTERY,
            METRIC_ALARMS,
            METRIC_BATTERY_POWER,
        ):
            # Sample-only metrics. Align the bucketing thresholds with
            # the daily-rollup family below so switching tabs over the
            # same date range keeps the same x-axis:
            #   ≤ 7 days        → raw 5-min line (rich intra-day shape)
            #   8 .. 31 days    → daily averages
            #   32 .. 732 days  → monthly averages
            #   > 732 days      → yearly averages
            if span_days <= 7:
                series = await self._range_samples(
                    station_id, start_date, end_date, metric=metric
                )
                return series, "5-min samples", start_date, end_date
            if span_days <= 31:
                series = await self._range_samples_aggregated(
                    station_id, start_date, end_date,
                    metric=metric, bucket="daily",
                )
                return series, "daily averages", start_date, end_date
            if span_days <= 732:
                series = await self._range_samples_aggregated(
                    station_id, start_date, end_date,
                    metric=metric, bucket="monthly",
                )
                return series, "monthly averages", start_date, end_date
            series = await self._range_samples_aggregated(
                station_id, start_date, end_date,
                metric=metric, bucket="yearly",
            )
            return series, "yearly averages", start_date, end_date

        if metric == METRIC_TOTAL_OUTPUT:
            # Run the Energy aggregation for the requested range, then
            # convert per-bucket values into a running sum. Result is a
            # monotonically-climbing curve showing how the station's
            # cumulative output has grown through the period.
            base, resolution, eff_start, eff_end = await self.auto_range(
                station_id,
                start_date,
                end_date,
                metric=METRIC_ENERGY,
            )
            running = 0.0
            cumulative: list[Point] = []
            for p in base.points:
                if p.v is not None:
                    running += p.v
                cumulative.append(Point(t=p.t, v=running))
            return (
                Series(
                    label="Cumulative production",
                    unit=base.unit,
                    points=cumulative,
                ),
                resolution,
                eff_start,
                eff_end,
            )

        if metric == METRIC_CASHFLOW:
            if span_days <= 31:
                series = await self._range_cashflow_daily(
                    station_id, start_date, end_date,
                    feed_in_tariff=feed_in_tariff,
                    import_tariff=import_tariff,
                    currency=currency,
                )
                return series, "daily totals", start_date, end_date
            bucket = 7 if span_days <= 732 else 4
            label = (
                "Monthly cashflow" if bucket == 7 else "Annual cashflow"
            )
            resolution = "monthly totals" if bucket == 7 else "yearly totals"
            series = await self._range_cashflow(
                station_id, start_date, end_date,
                feed_in_tariff=feed_in_tariff,
                import_tariff=import_tariff,
                currency=currency,
                bucket_chars=bucket,
                label=label,
            )
            return series, resolution, start_date, end_date

        if metric == METRIC_NET:
            if span_days <= 31:
                series = await self._range_net_daily(
                    station_id, start_date, end_date
                )
                return series, "daily totals", start_date, end_date
            if span_days <= 732:
                series = await self._range_net_aggregate(
                    station_id,
                    start_date,
                    end_date,
                    bucket_chars=7,
                    label="Monthly net (export - import)",
                )
                return series, "monthly totals", start_date, end_date
            series = await self._range_net_aggregate(
                station_id,
                start_date,
                end_date,
                bucket_chars=4,
                label="Annual net (export - import)",
            )
            return series, "yearly totals", start_date, end_date

        if metric not in _DAILY_METRIC_FIELDS:
            raise ValueError(f"unsupported metric: {metric!r}")

        if span_days <= 31:
            series = await self._range_daily(
                station_id, start_date, end_date, metric=metric
            )
            return series, "daily totals", start_date, end_date
        if span_days <= 732:  # ~2 years
            series = await self._range_aggregate(
                station_id,
                start_date,
                end_date,
                metric=metric,
                bucket_chars=7,
                label_attr="yearly_label",
            )
            return series, "monthly totals", start_date, end_date
        series = await self._range_aggregate(
            station_id,
            start_date,
            end_date,
            metric=metric,
            bucket_chars=4,
            label_attr="alltime_label",
        )
        return series, "yearly totals", start_date, end_date

    # --- live-tile sparklines ----------------------------------------------

    async def recent_samples(
        self, station_id: str, *, hours: int = 24, fields: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """Return up to `hours` of recent `station_samples` rows, oldest first.

        Powers the sparklines under the home-page live tiles. Keep the
        projection narrow so we don't ship 100KB of unused detail to the
        template just to draw four 60-pixel lines.
        """

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
