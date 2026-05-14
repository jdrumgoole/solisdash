"""Alarm-feed queries — MongoDB only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

ALARM_STATE_LABELS = {
    "0": "pending",
    "1": "processed",
    "2": "restored",
}


@dataclass(frozen=True)
class AlarmPage:
    """One paginated slice of the `alarms` collection."""

    rows: list[dict[str, Any]]
    total: int
    page_no: int
    page_size: int

    @property
    def has_next(self) -> bool:
        return self.page_no * self.page_size < self.total


class AlarmService:
    """Read-only listing over the `alarms` collection."""

    def __init__(self, db: AsyncDatabase[dict[str, Any]]) -> None:
        self._db = db

    async def list_alarms(
        self,
        *,
        page_no: int = 1,
        page_size: int = 25,
        station_id: str | None = None,
        state: str | None = None,
    ) -> AlarmPage:
        page_no = max(1, page_no)
        page_size = max(1, min(page_size, 100))

        query: dict[str, Any] = {}
        if station_id:
            query["station_id"] = station_id
        if state:
            query["state"] = state

        total = await self._db["alarms"].count_documents(query)
        cursor = (
            self._db["alarms"]
            .find(query, sort=[("alarm_begin_time", -1)])
            .skip((page_no - 1) * page_size)
            .limit(page_size)
        )
        rows = [doc async for doc in cursor]
        return AlarmPage(
            rows=rows, total=total, page_no=page_no, page_size=page_size
        )
