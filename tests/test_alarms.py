from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from solisdash.alarms import ALARM_STATE_LABELS, AlarmPage, AlarmService


def test_alarm_state_labels_match_spec() -> None:
    # V2.0.3 §3.9 defines: 0 pending, 1 processed, 2 restored.
    assert ALARM_STATE_LABELS["0"] == "pending"
    assert ALARM_STATE_LABELS["1"] == "processed"
    assert ALARM_STATE_LABELS["2"] == "restored"


async def _seed(db: AsyncDatabase[dict[str, Any]]) -> None:
    polled = datetime(2026, 5, 14, 12, 0, tzinfo=UTC)
    await db["alarms"].insert_many(
        [
            {
                "id": f"A{i}",
                "station_id": "S1",
                "alarm_code": f"21{i:02d}",
                "alarm_begin_time": 1_000_000_000_000 + i,
                "state": "0" if i % 2 == 0 else "1",
                "polled_at": polled,
            }
            for i in range(5)
        ]
        + [
            {
                "id": "X",
                "station_id": "S2",
                "alarm_code": "9999",
                "alarm_begin_time": 1_000_000_000_999,
                "state": "0",
                "polled_at": polled,
            }
        ]
    )


async def test_list_alarms_default_returns_first_page_sorted_by_time(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await _seed(clean_db)
    page = await AlarmService(clean_db).list_alarms(page_no=1, page_size=10)
    assert isinstance(page, AlarmPage)
    assert page.total == 6
    # Sorted by alarm_begin_time descending — the S2 row + the S1 rows in reverse i.
    assert page.rows[0]["id"] == "X"
    assert page.rows[1]["id"] == "A4"
    assert page.rows[-1]["id"] == "A0"
    assert page.has_next is False


async def test_list_alarms_filters_by_station(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await _seed(clean_db)
    page = await AlarmService(clean_db).list_alarms(station_id="S2")
    assert page.total == 1
    assert page.rows[0]["id"] == "X"


async def test_list_alarms_filters_by_state(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await _seed(clean_db)
    page = await AlarmService(clean_db).list_alarms(state="0")
    # S1 even-indexed (3 rows) + S2 row.
    assert {r["id"] for r in page.rows} == {"A0", "A2", "A4", "X"}


async def test_list_alarms_paginates_and_signals_next_page(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await _seed(clean_db)
    p1 = await AlarmService(clean_db).list_alarms(page_no=1, page_size=2)
    assert p1.total == 6
    assert len(p1.rows) == 2
    assert p1.has_next is True

    p2 = await AlarmService(clean_db).list_alarms(page_no=2, page_size=2)
    assert {r["id"] for r in p2.rows} & {r["id"] for r in p1.rows} == set()

    p_last = await AlarmService(clean_db).list_alarms(page_no=3, page_size=2)
    assert p_last.has_next is False


async def test_list_alarms_clamps_invalid_page_inputs(
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    await _seed(clean_db)
    service = AlarmService(clean_db)
    p = await service.list_alarms(page_no=0, page_size=0)
    assert p.page_no == 1
    assert p.page_size == 1

    p = await service.list_alarms(page_no=1, page_size=10_000)
    assert p.page_size == 100  # capped at 100
