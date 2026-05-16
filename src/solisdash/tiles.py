"""Live-tile data: pulls station detail from SolisCloud through a short
TTL cache, falls back to the last known `station_samples` row when the
upstream call is rate-limited or otherwise fails.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, TypeVar

from pymongo.asynchronous.database import AsyncDatabase

from solisdash.client import RETRYABLE_CODES, SolisAPIError, SolisClient

T = TypeVar("T")

STATIONS_TTL_S = 15 * 60.0   # station list rarely changes
TILES_TTL_S = 45.0           # tiles refresh every 30s on the page


class TTLCache:
    """Single-process async TTL cache.

    Concurrent calls for the same key collapse onto one factory invocation
    so a refresh storm cannot multiply outbound traffic.
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._store: dict[Hashable, tuple[float, Any]] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}

    def _lock(self, key: Hashable) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get_or_set(
        self,
        key: Hashable,
        factory: Callable[[], Awaitable[T]],
    ) -> T:
        now = time.monotonic()
        cached = self._store.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]  # type: ignore[no-any-return]

        async with self._lock(key):
            # Re-check under the lock so the second caller picks up the first
            # caller's value rather than re-running the factory.
            now = time.monotonic()
            cached = self._store.get(key)
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]  # type: ignore[no-any-return]
            value = await factory()
            self._store[key] = (time.monotonic(), value)
            return value

    def invalidate(self, key: Hashable) -> None:
        self._store.pop(key, None)


@dataclass(frozen=True)
class TilesData:
    """Snapshot rendered on the home dashboard."""

    station_id: str
    station_name: str
    current_power: float | None
    current_power_unit: str
    today_energy: float | None
    today_energy_unit: str
    month_energy: float | None
    month_energy_unit: str
    battery_soc_pct: float | None
    alarm_count: int | None
    data_ts: datetime | None
    stale: bool = False
    error: str | None = None
    # Pre-rendered SVG `d` strings for the inline sparklines under each
    # tile. Each path is for a 200x40 viewBox. None when no recent data.
    power_sparkline: str | None = None
    today_energy_sparkline: str | None = None
    month_energy_sparkline: str | None = None
    battery_sparkline: str | None = None
    alarms_sparkline: str | None = None


def sparkline_path(
    values: list[float | None], *, width: int = 200, height: int = 40, pad: int = 2
) -> str | None:
    """Return an SVG `d` attribute for a sparkline over `values`.

    Returns None when there are fewer than two real points — sparklines
    only make sense once there's a trend to show.
    """
    real = [v for v in values if v is not None]
    if len(real) < 2:
        return None
    lo, hi = min(real), max(real)
    span = (hi - lo) or 1.0
    # Replace gaps with the running last value so the path doesn't dive.
    filled: list[float] = []
    last = real[0]
    for v in values:
        if v is not None:
            last = v
        filled.append(last)
    n = len(filled)
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad

    def x(i: int) -> float:
        return pad + inner_w * i / (n - 1) if n > 1 else pad

    def y(v: float) -> float:
        return pad + inner_h - ((v - lo) / span) * inner_h

    pts = [f"{x(i):.2f},{y(v):.2f}" for i, v in enumerate(filled)]
    return "M" + " L".join(pts)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_minus(*, hours: int) -> datetime:
    from datetime import timedelta

    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _ms_to_datetime(value: Any) -> datetime | None:
    """SolisCloud returns timestamps as ms since epoch (or strings of same)."""
    ms = _as_int(value)
    if ms is None or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def parse_station_detail(detail: dict[str, Any], alarm_count: int | None) -> TilesData:
    """Pluck the tile fields out of a `/v1/api/stationDetail` payload.

    Field names follow V2.0.3 §4.2; the API doc marks most return fields as
    optional, so each pull tolerates missing keys without raising.
    """
    return TilesData(
        station_id=str(detail.get("id", "")),
        station_name=str(detail.get("stationName") or detail.get("addr") or ""),
        current_power=_as_float(detail.get("psum") or detail.get("power")),
        current_power_unit=str(detail.get("psumStr") or detail.get("powerStr") or "kW"),
        today_energy=_as_float(detail.get("dayEnergy")),
        today_energy_unit=str(detail.get("dayEnergyStr") or "kWh"),
        month_energy=_as_float(detail.get("monthEnergy")),
        month_energy_unit=str(detail.get("monthEnergyStr") or "kWh"),
        battery_soc_pct=_as_float(detail.get("batteryPercent")),
        alarm_count=alarm_count,
        data_ts=_ms_to_datetime(detail.get("dataTimestamp")),
    )


def from_sample(sample: dict[str, Any], station_name: str) -> TilesData:
    """Build a tile snapshot from a `station_samples` doc as a stale fallback."""
    ts = sample.get("ts")
    data_ts: datetime | None
    if isinstance(ts, datetime):
        data_ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    else:
        data_ts = _ms_to_datetime(ts)
    return TilesData(
        station_id=str(sample.get("station_id", "")),
        station_name=station_name,
        current_power=_as_float(sample.get("psum") or sample.get("power")),
        current_power_unit=str(sample.get("power_unit") or "kW"),
        today_energy=_as_float(sample.get("day_energy")),
        today_energy_unit=str(sample.get("day_energy_unit") or "kWh"),
        month_energy=_as_float(sample.get("month_energy")),
        month_energy_unit=str(sample.get("month_energy_unit") or "kWh"),
        battery_soc_pct=_as_float(sample.get("battery_soc")),
        alarm_count=_as_int(sample.get("alarm_count")),
        data_ts=data_ts,
        stale=True,
    )


class LiveTilesService:
    """Compose `SolisClient` + `station_samples` into a tile snapshot.

    Two short-lived TTL caches sit in front of SolisCloud: one for the
    station list (15 minutes, since it changes rarely) and one for tile
    data per station (45 seconds, so the home page refreshing every
    30 seconds doesn't outpace the upstream rate limit).
    """

    def __init__(
        self,
        solis: SolisClient,
        db: AsyncDatabase[dict[str, Any]],
        *,
        default_station_id: str | None = None,
        tiles_ttl: float = TILES_TTL_S,
        stations_ttl: float = STATIONS_TTL_S,
    ) -> None:
        self._solis = solis
        self._db = db
        self._default_station_id = default_station_id
        self._stations_cache: TTLCache = TTLCache(stations_ttl)
        self._tiles_cache: TTLCache = TTLCache(tiles_ttl)

    async def default_station_id(self) -> str | None:
        if self._default_station_id:
            return self._default_station_id

        async def _fetch() -> str | None:
            page = await self._solis.user_station_list(page_no=1, page_size=1)
            if not page.records:
                return None
            return str(page.records[0].get("id") or "") or None

        return await self._stations_cache.get_or_set("default", _fetch)

    async def get_tiles(self, station_id: str) -> TilesData:
        async def _fetch() -> TilesData:
            return await self._fetch_fresh(station_id)

        try:
            tiles = await self._tiles_cache.get_or_set(station_id, _fetch)
        except SolisAPIError as exc:
            if exc.code in RETRYABLE_CODES:
                fallback = await self._last_known(station_id)
                if fallback is not None:
                    fallback = replace(fallback, error=f"rate limited ({exc.code})")
                    return await self._with_sparklines(station_id, fallback)
            raise
        return await self._with_sparklines(station_id, tiles)

    async def _with_sparklines(
        self, station_id: str, tiles: TilesData
    ) -> TilesData:
        """Attach 24h sparkline path strings.

        Uncached — the underlying query is cheap (one indexed range scan)
        and the tiles page refreshes every 30s, so we want the most recent
        trend each time. If the lookup fails for any reason we return the
        tiles unchanged so the page still renders.
        """
        try:
            cursor = self._db["station_samples"].find(
                {
                    "station_id": station_id,
                    "ts": {"$gte": _now_minus(hours=24)},
                },
                sort=[("ts", 1)],
                projection={
                    "_id": 0,
                    "psum": 1,
                    "power": 1,
                    "day_energy": 1,
                    "month_energy": 1,
                    "battery_soc": 1,
                    "alarm_count": 1,
                },
            )
            rows = [doc async for doc in cursor]
        except Exception:
            return tiles
        if not rows:
            return tiles
        return replace(
            tiles,
            power_sparkline=sparkline_path(
                [_as_float(r.get("psum") or r.get("power")) for r in rows]
            ),
            today_energy_sparkline=sparkline_path(
                [_as_float(r.get("day_energy")) for r in rows]
            ),
            month_energy_sparkline=sparkline_path(
                [_as_float(r.get("month_energy")) for r in rows]
            ),
            battery_sparkline=sparkline_path(
                [_as_float(r.get("battery_soc")) for r in rows]
            ),
            alarms_sparkline=sparkline_path(
                [
                    float(c) if (c := _as_int(r.get("alarm_count"))) is not None else None
                    for r in rows
                ]
            ),
        )

    async def _fetch_fresh(self, station_id: str) -> TilesData:
        detail = await self._solis.station_detail(station_id=station_id)
        alarm_count: int | None
        try:
            alarms = await self._solis.alarm_list(
                page_no=1, page_size=1, station_id=station_id, state=0
            )
            alarm_count = alarms.total
        except SolisAPIError:
            alarm_count = None
        return parse_station_detail(detail, alarm_count)

    async def _last_known(self, station_id: str) -> TilesData | None:
        sample = await self._db["station_samples"].find_one(
            {"station_id": station_id},
            sort=[("ts", -1)],
        )
        if sample is None:
            return None
        station_name = ""
        station_doc = await self._db["stations"].find_one({"id": station_id})
        if station_doc is not None:
            station_name = str(station_doc.get("stationName") or "")
        return from_sample(sample, station_name)
