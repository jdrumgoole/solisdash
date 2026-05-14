from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from solisdash.client import (
    RETRYABLE_CODES,
    Page,
    SolisAPIError,
    SolisClient,
)
from solisdash.signing import build_headers

BASE_URL = "https://api.example.invalid:13333"
KEY_ID = "test-key-id"
KEY_SECRET = "test-secret"


def _envelope(data: Any, code: str = "0", msg: str = "success") -> dict[str, Any]:
    return {"success": code == "0", "code": code, "msg": msg, "data": data}


def _ok_json(data: Any) -> httpx.Response:
    return httpx.Response(200, json=_envelope(data))


def _page(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "page": {
            "records": records,
            "total": len(records),
            "size": 20,
            "current": 1,
            "pages": 1,
        }
    }


class Recorder:
    """Captures every outbound request and returns scripted responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content) if request.content else {})
        if not self._responses:
            raise AssertionError(f"unexpected request to {request.url.path}")
        return self._responses.pop(0)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def fake_sleep(sleeps: list[float]) -> Callable[[float], Any]:
    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return _sleep


def make_client(
    recorder: Recorder,
    *,
    sleep: Callable[[float], Any] | None = None,
    max_retries: int = 3,
) -> SolisClient:
    return SolisClient(
        base_url=BASE_URL,
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
        transport=httpx.MockTransport(recorder),
        sleep=sleep if sleep is not None else __import__("asyncio").sleep,
        max_retries=max_retries,
        backoff_initial=0.0,
        backoff_max=0.0,
    )


# --- signing / headers -----------------------------------------------------


async def test_request_carries_signed_headers_and_keyid() -> None:
    recorder = Recorder([_ok_json(_page([]))])
    async with make_client(recorder) as client:
        await client.user_station_list(page_no=1, page_size=10)

    req = recorder.requests[0]
    assert req.method == "POST"
    assert req.url.path == "/v1/api/userStationList"
    # Spec required headers (no separate KeyId — apiId travels in Authorization).
    for required in ("Content-MD5", "Content-Type", "Date", "Authorization"):
        assert required in req.headers
    assert req.headers["Authorization"].startswith(f"API {KEY_ID}:")
    assert req.headers["Content-Type"] == "application/json"

    # Independently sign the exact bytes sent and compare.
    expected = build_headers(
        path="/v1/api/userStationList",
        body=req.content,
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
    )
    assert req.headers["Content-MD5"] == expected["Content-MD5"]
    # Authorization must match because Content-MD5/Date are reused above.
    # We can't pin Date across calls, so re-sign with the actual Date header:
    expected_resigned = build_headers(
        path="/v1/api/userStationList",
        body=req.content,
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
        when=__import__("email").utils.parsedate_to_datetime(req.headers["Date"]),
    )
    assert req.headers["Authorization"] == expected_resigned["Authorization"]


async def test_post_body_is_compact_json_without_none_values() -> None:
    recorder = Recorder([_ok_json(_page([]))])
    async with make_client(recorder) as client:
        await client.user_station_list(page_no=2, page_size=50)

    body = recorder.bodies[0]
    assert body == {"pageNo": 2, "pageSize": 50}
    assert "nmiCode" not in body
    assert "idList" not in body


# --- envelope handling -----------------------------------------------------


async def test_success_envelope_unwraps_data() -> None:
    records = [{"id": "1", "stationName": "Roof"}, {"id": "2", "stationName": "Barn"}]
    recorder = Recorder([_ok_json(_page(records))])
    async with make_client(recorder) as client:
        page = await client.user_station_list()
    assert isinstance(page, Page)
    assert page.records == records
    assert page.total == 2


async def test_non_zero_code_raises_solis_api_error() -> None:
    response = httpx.Response(200, json=_envelope({}, code="B0011", msg="user not found"))
    recorder = Recorder([response])
    async with make_client(recorder) as client:
        with pytest.raises(SolisAPIError) as ei:
            await client.user_station_list()
    assert ei.value.code == "B0011"
    assert ei.value.msg == "user not found"


# --- rate-limit / retry ----------------------------------------------------


@pytest.mark.parametrize("code", sorted(RETRYABLE_CODES))
async def test_rate_limit_code_retries_then_succeeds(
    code: str,
    sleeps: list[float],
    fake_sleep: Callable[[float], Any],
) -> None:
    recorder = Recorder(
        [
            httpx.Response(200, json=_envelope({}, code=code, msg="rate limited")),
            httpx.Response(200, json=_envelope({}, code=code, msg="rate limited")),
            _ok_json(_page([])),
        ]
    )
    async with make_client(recorder, sleep=fake_sleep) as client:
        page = await client.user_station_list()
    assert page.records == []
    assert len(recorder.requests) == 3
    assert len(sleeps) == 2  # one sleep per retry


async def test_rate_limit_exhausts_retries_and_raises(
    sleeps: list[float],
    fake_sleep: Callable[[float], Any],
) -> None:
    recorder = Recorder(
        [
            httpx.Response(200, json=_envelope({}, code="1004", msg="rate limited"))
            for _ in range(4)
        ]
    )
    async with make_client(recorder, sleep=fake_sleep, max_retries=3) as client:
        with pytest.raises(SolisAPIError) as ei:
            await client.user_station_list()
    assert ei.value.code == "1004"
    assert len(recorder.requests) == 4
    assert len(sleeps) == 3


async def test_http_429_retries(sleeps: list[float], fake_sleep: Callable[[float], Any]) -> None:
    recorder = Recorder(
        [
            httpx.Response(429),
            _ok_json(_page([])),
        ]
    )
    async with make_client(recorder, sleep=fake_sleep) as client:
        page = await client.user_station_list()
    assert page.total == 0
    assert len(sleeps) == 1


async def test_non_retryable_error_code_does_not_retry(
    sleeps: list[float],
    fake_sleep: Callable[[float], Any],
) -> None:
    recorder = Recorder([httpx.Response(200, json=_envelope({}, code="I0000", msg="bad params"))])
    async with make_client(recorder, sleep=fake_sleep) as client:
        with pytest.raises(SolisAPIError):
            await client.user_station_list()
    assert len(recorder.requests) == 1
    assert sleeps == []


# --- endpoint body shapes --------------------------------------------------


async def test_station_detail_requires_id_or_nmi_code() -> None:
    recorder = Recorder([])
    async with make_client(recorder) as client:
        with pytest.raises(ValueError, match="station_detail requires"):
            await client.station_detail()
    assert recorder.requests == []


async def test_station_detail_sends_id_and_returns_data_object() -> None:
    recorder = Recorder([_ok_json({"id": "S1", "dayEnergy": 12.3})])
    async with make_client(recorder) as client:
        data = await client.station_detail(station_id="S1")
    assert data == {"id": "S1", "dayEnergy": 12.3}
    assert recorder.bodies[0] == {"id": "S1"}
    assert recorder.requests[0].url.path == "/v1/api/stationDetail"


async def test_station_day_body_uses_spec_field_names() -> None:
    recorder = Recorder([_ok_json([{"time": 1685057100000, "power": 77.0}])])
    async with make_client(recorder) as client:
        series = await client.station_day(
            station_id="S1",
            money="EUR",
            time="2025-06-12",
            time_zone=1,
        )
    assert series == [{"time": 1685057100000, "power": 77.0}]
    assert recorder.bodies[0] == {
        "id": "S1",
        "money": "EUR",
        "time": "2025-06-12",
        "timeZone": 1,
    }
    assert recorder.requests[0].url.path == "/v1/api/stationDay"


async def test_inverter_list_sends_pagination_and_filters() -> None:
    recorder = Recorder([_ok_json(_page([{"sn": "ABC"}]))])
    async with make_client(recorder) as client:
        page = await client.inverter_list(
            page_no=3,
            page_size=50,
            station_id="S1",
            sn_list=["ABC", "DEF"],
        )
    assert page.records == [{"sn": "ABC"}]
    assert recorder.bodies[0] == {
        "pageNo": 3,
        "pageSize": 50,
        "stationId": "S1",
        "snList": ["ABC", "DEF"],
    }


async def test_inverter_detail_accepts_sn_alone() -> None:
    recorder = Recorder([_ok_json({"sn": "XYZ", "pac": 4.2})])
    async with make_client(recorder) as client:
        data = await client.inverter_detail(sn="XYZ")
    assert data["sn"] == "XYZ"
    assert recorder.bodies[0] == {"sn": "XYZ"}


async def test_alarm_list_sends_full_filter_set() -> None:
    recorder = Recorder([_ok_json(_page([]))])
    async with make_client(recorder) as client:
        await client.alarm_list(
            page_no=1,
            page_size=10,
            station_id="S1",
            alarm_device_sn="ABC",
            alarm_begin_time="2025-01-01",
            alarm_end_time="2025-01-31",
            state=0,
        )
    assert recorder.bodies[0] == {
        "pageNo": 1,
        "pageSize": 10,
        "stationId": "S1",
        "alarmDeviceSn": "ABC",
        "alarmBeginTime": "2025-01-01",
        "alarmEndTime": "2025-01-31",
        "state": 0,
    }
    assert recorder.requests[0].url.path == "/v1/api/alarmList"


async def test_alarm_list_handles_unwrapped_data_shape() -> None:
    """alarmList puts page fields directly under `data`, with no `page` key."""
    unwrapped = {
        "records": [{"alarm_code": "2129"}, {"alarm_code": "2130"}],
        "total": 2,
        "size": 20,
        "current": 1,
        "pages": 1,
    }
    recorder = Recorder([httpx.Response(200, json=_envelope(unwrapped))])
    async with make_client(recorder) as client:
        page = await client.alarm_list(page_no=1, page_size=20, station_id="S1")
    assert page.total == 2
    assert [r["alarm_code"] for r in page.records] == ["2129", "2130"]


async def test_alarm_list_still_handles_wrapped_data_shape() -> None:
    """Belt and braces: tolerate the spec's parameter-table shape too."""
    recorder = Recorder([_ok_json(_page([{"alarm_code": "2131"}]))])
    async with make_client(recorder) as client:
        page = await client.alarm_list(page_no=1, page_size=20, station_id="S1")
    assert page.total == 1
    assert page.records[0]["alarm_code"] == "2131"


# --- lifecycle -------------------------------------------------------------


async def test_client_path_must_start_with_slash() -> None:
    recorder = Recorder([])
    client = SolisClient(
        base_url=BASE_URL,
        key_id=KEY_ID,
        key_secret=KEY_SECRET,
        transport=httpx.MockTransport(recorder),
    )
    try:
        with pytest.raises(ValueError, match="must start with"):
            await client._post("v1/api/oops", {})
    finally:
        await client.aclose()
