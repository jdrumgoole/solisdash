"""Build the APScheduler instance the FastAPI lifespan owns.

Two jobs:
- `sample`  — pull `stationDetail` for every station into `station_samples`
              every `SCHEDULER_SAMPLE_MINUTES` minutes.
- `daily`   — pull this month's `stationMonth` and upsert daily totals at
              `SCHEDULER_DAILY_HOUR_UTC:SCHEDULER_DAILY_MINUTE_UTC` UTC.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from solisdash.config import Settings
from solisdash.poller import Poller

log = logging.getLogger("solisdash.scheduler")


def build_scheduler(poller: Poller, settings: Settings) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=UTC)

    async def _sample() -> None:
        log.info("scheduler: sample run starting")
        try:
            n = await poller.poll_current_all()
            log.info("scheduler: sample run wrote %d snapshots", n)
        except Exception:
            log.exception("scheduler: sample run failed")
        try:
            n = await poller.poll_alarms_all()
            log.info("scheduler: alarm run upserted %d rows", n)
        except Exception:
            log.exception("scheduler: alarm run failed")

    async def _daily() -> None:
        log.info("scheduler: daily run starting")
        try:
            month = datetime.now(UTC).strftime("%Y-%m")
            for sid in await poller.list_station_ids():
                await poller.poll_daily_for_month(sid, month)
            log.info("scheduler: daily run done for month %s", month)
        except Exception:
            log.exception("scheduler: daily run failed")

    scheduler.add_job(
        _sample,
        trigger=IntervalTrigger(minutes=settings.SCHEDULER_SAMPLE_MINUTES),
        id="sample",
        next_run_time=datetime.now(UTC),  # first run on startup
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _daily,
        trigger=CronTrigger(
            hour=settings.SCHEDULER_DAILY_HOUR_UTC,
            minute=settings.SCHEDULER_DAILY_MINUTE_UTC,
        ),
        id="daily",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
