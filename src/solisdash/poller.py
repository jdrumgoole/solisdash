"""Scheduled and one-shot data pulls from SolisCloud into MongoDB.

`Poller.poll_current` writes one snapshot of every station's `stationDetail`
into `station_samples` — that's what the live-tile fallback reads when the
upstream API is rate-limited. `Poller.poll_daily_for_month` upserts the
per-day rollups returned by `stationMonth` into `station_daily`.

Pacing is via :class:`solisdash.ratelimit.TokenBucket` so the scheduler and
on-demand routes share one shared budget against the per-key cap.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from solisdash.client import SolisAPIError, SolisClient
from solisdash.ratelimit import TokenBucket
from solisdash.tiles import _as_float, _as_int, _ms_to_datetime

log = logging.getLogger("solisdash.poller")


@dataclass(frozen=True)
class PollerSettings:
    """Tuning knobs surfaced to the lifespan / CLI."""

    sample_interval_minutes: int = 5
    daily_rollup_hour_utc: int = 0
    daily_rollup_minute_utc: int = 30
    rate_per_sec: float = 1.5  # ≤ SolisCloud's 2/sec per-endpoint cap
    burst: float = 2.0


def _detail_to_sample(
    station_id: str, detail: dict[str, Any], polled_at: datetime
) -> dict[str, Any]:
    """Project a `stationDetail` payload into a `station_samples` doc.

    Field names match those :func:`solisdash.tiles.from_sample` reads when
    the live tiles fall back to the most recent stored sample.
    """
    ts = _ms_to_datetime(detail.get("dataTimestamp")) or polled_at
    return {
        "station_id": station_id,
        "ts": ts,
        "psum": _as_float(detail.get("psum") or detail.get("power")),
        "power": _as_float(detail.get("power")),
        "power_unit": str(detail.get("psumStr") or detail.get("powerStr") or "kW"),
        "day_energy": _as_float(detail.get("dayEnergy")),
        "day_energy_unit": str(detail.get("dayEnergyStr") or "kWh"),
        "month_energy": _as_float(detail.get("monthEnergy")),
        "month_energy_unit": str(detail.get("monthEnergyStr") or "kWh"),
        "battery_soc": _as_float(detail.get("batteryPercent")),
        "alarm_count": _as_int(detail.get("warningInfoData")),
        "polled_at": polled_at,
    }


def _row_to_daily(station_id: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """Project one element of a `stationMonth` payload into a `station_daily` doc."""
    iso = row.get("dateStr") or row.get("date")
    if iso is None:
        return None
    if isinstance(iso, int | float):
        when = datetime.fromtimestamp(int(iso) / 1000, tz=timezone.utc).date().isoformat()
    else:
        when = str(iso)[:10]
    return {
        "station_id": station_id,
        "date": when,
        "energy": _as_float(row.get("energy")),
        "energy_unit": str(row.get("energyStr") or "kWh"),
        "money": _as_float(row.get("money")),
        "money_unit": str(row.get("moneyStr") or ""),
        "full_hour": _as_float(row.get("fullHour")),
    }


def _alarm_to_doc(
    station_id: str, row: dict[str, Any], polled_at: datetime
) -> dict[str, Any] | None:
    """Project one element of an `alarmList` payload into an `alarms` doc.

    `alarmList` rows carry an upstream `id` string we use as the unique key.
    Rows without an id are skipped — they'd collide with each other on upsert.
    """
    alarm_id = row.get("id")
    if alarm_id is None:
        return None
    return {
        "id": str(alarm_id),
        "station_id": station_id,
        "alarm_device_sn": str(row.get("alarmDeviceSn") or ""),
        "alarm_device_type": _as_int(row.get("alarmDeviceType")),
        "alarm_type": _as_int(row.get("alarmType")),
        "alarm_level": str(row.get("alarmLevel") or ""),
        "alarm_code": str(row.get("alarmCode") or ""),
        "alarm_begin_time": _as_int(row.get("alarmBeginTime")),
        "alarm_end_time": _as_int(row.get("alarmEndTime")),
        "alarm_msg": str(row.get("alarmMsg") or ""),
        "advice": str(row.get("advice") or ""),
        "state": str(row.get("state") or ""),
        "model": str(row.get("model") or ""),
        "polled_at": polled_at,
    }


def iter_months(start: date, end: date) -> Iterator[str]:
    """Yield YYYY-MM strings from start.month through end.month inclusive."""
    if end < start:
        raise ValueError(f"end {end!r} is before start {start!r}")
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            m = 1
            y += 1


class Poller:
    """SolisCloud → MongoDB pump.

    Holds references to a shared `SolisClient` and `AsyncDatabase`. All
    outbound calls are gated by a `TokenBucket`; errors are logged but
    never escape (the scheduler must keep running).
    """

    def __init__(
        self,
        *,
        solis: SolisClient,
        db: AsyncDatabase[dict[str, Any]],
        money: str = "EUR",
        time_zone: int = 0,
        rate_limiter: TokenBucket | None = None,
    ) -> None:
        self._solis = solis
        self._db = db
        self._money = money
        self._time_zone = time_zone
        self._limiter = rate_limiter or TokenBucket(rate=1.5, capacity=2.0)

    async def list_station_ids(self) -> list[str]:
        await self._limiter.acquire()
        page = await self._solis.user_station_list(page_no=1, page_size=100)
        return [str(r.get("id") or "") for r in page.records if r.get("id")]

    async def poll_current(self, station_id: str) -> dict[str, Any] | None:
        """Pull one stationDetail and upsert it into `stations` + `station_samples`."""
        await self._limiter.acquire()
        try:
            detail = await self._solis.station_detail(station_id=station_id)
        except SolisAPIError as exc:
            log.warning("station_detail %s failed: %s", station_id, exc)
            return None

        polled_at = datetime.now(timezone.utc)
        sample = _detail_to_sample(station_id, detail, polled_at)
        await self._db["station_samples"].insert_one(sample)
        await self._db["stations"].update_one(
            {"id": station_id},
            {
                "$set": {
                    "id": station_id,
                    "stationName": detail.get("stationName"),
                    "addr": detail.get("addr"),
                    "capacity": detail.get("capacity"),
                    "last_seen_at": polled_at,
                }
            },
            upsert=True,
        )
        return sample

    async def poll_current_all(self) -> int:
        """Run poll_current for every station; return how many succeeded."""
        ok = 0
        for sid in await self.list_station_ids():
            if await self.poll_current(sid) is not None:
                ok += 1
        return ok

    async def poll_daily_for_month(self, station_id: str, month: str) -> int:
        """Upsert daily rollups for `month` (YYYY-MM). Returns rows written."""
        await self._limiter.acquire()
        try:
            rows = await self._solis.station_month(
                station_id=station_id,
                money=self._money,
                month=month,
                time_zone=self._time_zone,
            )
        except SolisAPIError as exc:
            log.warning(
                "station_month %s %s failed: %s", station_id, month, exc
            )
            return 0

        written = 0
        for row in rows:
            doc = _row_to_daily(station_id, row)
            if doc is None:
                continue
            await self._db["station_daily"].update_one(
                {"station_id": doc["station_id"], "date": doc["date"]},
                {"$set": doc},
                upsert=True,
            )
            written += 1
        return written

    async def backfill_daily(
        self, *, start: date, end: date, station_ids: list[str] | None = None
    ) -> dict[str, int]:
        """Upsert daily rollups between `start` and `end` (inclusive)."""
        if station_ids is None:
            station_ids = await self.list_station_ids()
        counts: dict[str, int] = {}
        for sid in station_ids:
            written = 0
            for month in iter_months(start, end):
                written += await self.poll_daily_for_month(sid, month)
            counts[sid] = written
        return counts

    async def poll_alarms(self, station_id: str, *, page_size: int = 100) -> int:
        """Pull all open and processed alarms for one station; upsert by upstream id."""
        await self._limiter.acquire()
        try:
            page = await self._solis.alarm_list(
                page_no=1,
                page_size=page_size,
                station_id=station_id,
            )
        except SolisAPIError as exc:
            log.warning("alarm_list %s failed: %s", station_id, exc)
            return 0

        polled_at = datetime.now(timezone.utc)
        written = 0
        for record in page.records:
            doc = _alarm_to_doc(station_id, record, polled_at)
            if doc is None:
                continue
            await self._db["alarms"].update_one(
                {"id": doc["id"]}, {"$set": doc}, upsert=True
            )
            written += 1
        return written

    async def poll_alarms_all(self) -> int:
        """Run poll_alarms for every station; return total rows upserted."""
        total = 0
        for sid in await self.list_station_ids():
            total += await self.poll_alarms(sid)
        return total
