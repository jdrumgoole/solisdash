"""Async SolisCloud HTTP client.

Wraps the V2.0.3 platform API. All endpoints POST a JSON body, get signed via
``solisdash.signing.build_headers``, and return ``{success, code, msg, data}``.
Non-zero ``code`` raises :class:`SolisAPIError`. Codes ``1004`` and ``1007``
(and HTTP 429) are treated as rate-limit and retried with exponential backoff
+ jitter, bounded by ``max_retries``.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from solisdash.signing import CONTENT_TYPE_DEFAULT, build_headers

RETRYABLE_CODES: frozenset[str] = frozenset({"1004", "1007"})
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 502, 503, 504})

SleepFn = Callable[[float], Awaitable[None]]


class SolisAPIError(Exception):
    """Non-success envelope from SolisCloud. ``code`` is the upstream string."""

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


@dataclass(frozen=True)
class Page:
    """One page of a paginated list response (``data.page``)."""

    records: list[dict[str, Any]]
    total: int
    size: int
    current: int
    pages: int

    @classmethod
    def from_envelope(cls, page_obj: dict[str, Any]) -> Page:
        return cls(
            records=list(page_obj.get("records") or []),
            total=int(page_obj.get("total") or 0),
            size=int(page_obj.get("size") or 0),
            current=int(page_obj.get("current") or 0),
            pages=int(page_obj.get("pages") or 0),
        )


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None so the signed body stays minimal."""
    return {k: v for k, v in body.items() if v is not None}


class SolisClient:
    """Async SolisCloud API client.

    Use as an async context manager so the underlying ``httpx.AsyncClient`` is
    closed deterministically::

        async with SolisClient(base_url=..., key_id=..., key_secret=...) as c:
            page = await c.user_station_list(page_no=1, page_size=20)
    """

    def __init__(
        self,
        *,
        base_url: str,
        key_id: str,
        key_secret: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._key_id = key_id
        self._key_secret = key_secret
        self._max_retries = max_retries
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> SolisClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        if not path.startswith("/"):
            raise ValueError(f"path must start with '/': {path!r}")
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")

        last_error: SolisAPIError | httpx.HTTPError | None = None
        for attempt in range(self._max_retries + 1):
            headers = build_headers(
                path=path,
                body=payload,
                key_id=self._key_id,
                key_secret=self._key_secret,
                content_type=CONTENT_TYPE_DEFAULT,
            )
            try:
                response = await self._client.post(path, content=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise
                await self._sleep(self._backoff(attempt))
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                await self._sleep(self._backoff(attempt))
                continue
            response.raise_for_status()

            envelope = response.json()
            code = str(envelope.get("code", ""))
            msg = str(envelope.get("msg", ""))
            if code == "0" or envelope.get("success") is True:
                return envelope.get("data")

            if code in RETRYABLE_CODES and attempt < self._max_retries:
                last_error = SolisAPIError(code, msg)
                await self._sleep(self._backoff(attempt))
                continue
            raise SolisAPIError(code, msg)

        # Loop only exits via return/raise above; this satisfies the type checker.
        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int) -> float:
        delay = self._backoff_initial * (2.0**attempt)
        delay = min(delay, self._backoff_max)
        jitter: float = random.random()
        return delay + jitter

    # --- Plant endpoints -------------------------------------------------

    async def user_station_list(
        self,
        page_no: int = 1,
        page_size: int = 20,
        *,
        nmi_code: str | None = None,
        id_list: list[str] | None = None,
    ) -> Page:
        data = await self._post(
            "/v1/api/userStationList",
            _compact(
                {
                    "pageNo": page_no,
                    "pageSize": page_size,
                    "nmiCode": nmi_code,
                    "idList": id_list,
                }
            ),
        )
        return Page.from_envelope(data["page"])

    async def station_detail(
        self,
        *,
        station_id: int | str | None = None,
        nmi_code: str | None = None,
    ) -> dict[str, Any]:
        if station_id is None and nmi_code is None:
            raise ValueError("station_detail requires station_id or nmi_code")
        data = await self._post(
            "/v1/api/stationDetail",
            _compact({"id": station_id, "nmiCode": nmi_code}),
        )
        return data  # type: ignore[no-any-return]

    async def station_day(
        self,
        *,
        money: str,
        time: str,
        time_zone: int,
        station_id: int | str | None = None,
        nmi_code: str | None = None,
    ) -> list[dict[str, Any]]:
        if station_id is None and nmi_code is None:
            raise ValueError("station_day requires station_id or nmi_code")
        data = await self._post(
            "/v1/api/stationDay",
            _compact(
                {
                    "id": station_id,
                    "nmiCode": nmi_code,
                    "money": money,
                    "time": time,
                    "timeZone": time_zone,
                }
            ),
        )
        return list(data or [])

    async def station_month(
        self,
        *,
        money: str,
        month: str,
        time_zone: int,
        station_id: int | str | None = None,
        nmi_code: str | None = None,
    ) -> list[dict[str, Any]]:
        if station_id is None and nmi_code is None:
            raise ValueError("station_month requires station_id or nmi_code")
        data = await self._post(
            "/v1/api/stationMonth",
            _compact(
                {
                    "id": station_id,
                    "nmiCode": nmi_code,
                    "money": money,
                    "month": month,
                    "timeZone": time_zone,
                }
            ),
        )
        return list(data or [])

    async def station_year(
        self,
        *,
        money: str,
        year: str,
        time_zone: int,
        station_id: int | str | None = None,
        nmi_code: str | None = None,
    ) -> list[dict[str, Any]]:
        if station_id is None and nmi_code is None:
            raise ValueError("station_year requires station_id or nmi_code")
        data = await self._post(
            "/v1/api/stationYear",
            _compact(
                {
                    "id": station_id,
                    "nmiCode": nmi_code,
                    "money": money,
                    "year": year,
                    "timeZone": time_zone,
                }
            ),
        )
        return list(data or [])

    async def station_all(
        self,
        *,
        money: str,
        time_zone: int,
        station_id: int | str | None = None,
        nmi_code: str | None = None,
    ) -> list[dict[str, Any]]:
        if station_id is None and nmi_code is None:
            raise ValueError("station_all requires station_id or nmi_code")
        data = await self._post(
            "/v1/api/stationAll",
            _compact(
                {
                    "id": station_id,
                    "nmiCode": nmi_code,
                    "money": money,
                    "timeZone": time_zone,
                }
            ),
        )
        return list(data or [])

    # --- Device endpoints ------------------------------------------------

    async def inverter_list(
        self,
        page_no: int = 1,
        page_size: int = 20,
        *,
        station_id: int | str | None = None,
        nmi_code: str | None = None,
        sn_list: list[str] | None = None,
    ) -> Page:
        data = await self._post(
            "/v1/api/inverterList",
            _compact(
                {
                    "pageNo": page_no,
                    "pageSize": page_size,
                    "stationId": station_id,
                    "nmiCode": nmi_code,
                    "snList": sn_list,
                }
            ),
        )
        return Page.from_envelope(data["page"])

    async def inverter_detail(
        self,
        *,
        inverter_id: int | str | None = None,
        sn: str | None = None,
    ) -> dict[str, Any]:
        if inverter_id is None and sn is None:
            raise ValueError("inverter_detail requires inverter_id or sn")
        data = await self._post(
            "/v1/api/inverterDetail",
            _compact({"id": inverter_id, "sn": sn}),
        )
        return data  # type: ignore[no-any-return]

    async def alarm_list(
        self,
        page_no: int = 1,
        page_size: int = 20,
        *,
        station_id: int | str | None = None,
        alarm_device_sn: str | None = None,
        alarm_begin_time: str | None = None,
        alarm_end_time: str | None = None,
        nmi_code: str | None = None,
        state: int | None = None,
    ) -> Page:
        # alarmList returns the page object directly under `data` — no
        # `page` wrapper — even though §3.9's parameter table claims one.
        # The §3.9 example response matches what the live API actually
        # sends. Tolerate both shapes defensively.
        data = await self._post(
            "/v1/api/alarmList",
            _compact(
                {
                    "pageNo": page_no,
                    "pageSize": page_size,
                    "stationId": station_id,
                    "alarmDeviceSn": alarm_device_sn,
                    "alarmBeginTime": alarm_begin_time,
                    "alarmEndTime": alarm_end_time,
                    "nmiCode": nmi_code,
                    "state": state,
                }
            ),
        )
        page = data["page"] if isinstance(data, dict) and "page" in data else data
        return Page.from_envelope(page)
