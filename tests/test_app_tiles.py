from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.app import app, get_tiles_service
from solisdash.auth import create_user
from solisdash.client import SolisAPIError
from solisdash.tiles import TilesData


class StubTilesService:
    """Quacks like `LiveTilesService` for endpoint tests."""

    def __init__(
        self,
        *,
        default_id: str | None = "S1",
        tiles: TilesData | None = None,
        get_error: Exception | None = None,
        default_error: Exception | None = None,
    ) -> None:
        self._default_id = default_id
        self._tiles = tiles
        self._get_error = get_error
        self._default_error = default_error
        self.get_calls: list[str] = []

    async def default_station_id(self) -> str | None:
        if self._default_error is not None:
            raise self._default_error
        return self._default_id

    async def get_tiles(self, station_id: str) -> TilesData:
        self.get_calls.append(station_id)
        if self._get_error is not None:
            raise self._get_error
        assert self._tiles is not None
        return self._tiles


@pytest.fixture
async def tiles_client(
    clean_db: AsyncDatabase[dict[str, Any]],
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[httpx.AsyncClient, StubTilesService]]:
    """Authed client with a logged-in admin and a controllable tiles service."""
    from solisdash.app import get_db

    stub: StubTilesService = getattr(request, "param", None) or StubTilesService(
        tiles=_sample_tiles(),
    )

    async def _override_db() -> AsyncDatabase[dict[str, Any]]:
        return clean_db

    async def _override_tiles() -> StubTilesService:
        return stub

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_tiles_service] = _override_tiles
    try:
        await create_user(clean_db, username="joe", password="hunter2", role="admin")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as ac:
            r = await ac.post(
                "/login",
                data={"username": "joe", "password": "hunter2"},
                follow_redirects=False,
            )
            assert r.status_code == 303
            yield ac, stub
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_tiles_service, None)


def _sample_tiles(**overrides: Any) -> TilesData:
    defaults: dict[str, Any] = dict(
        station_id="S1",
        station_name="Roof",
        current_power=4.25,
        current_power_unit="kW",
        today_energy=31.3,
        today_energy_unit="kWh",
        month_energy=839.0,
        month_energy_unit="kWh",
        battery_soc_pct=78.0,
        alarm_count=2,
        data_ts=datetime(2026, 5, 14, 10, 30, tzinfo=timezone.utc),
        stale=False,
        error=None,
    )
    defaults.update(overrides)
    return TilesData(**defaults)


# --- Endpoint tests --------------------------------------------------------


async def test_home_renders_tile_values_inline(
    tiles_client: tuple[httpx.AsyncClient, StubTilesService],
) -> None:
    ac, _ = tiles_client
    r = await ac.get("/")
    assert r.status_code == 200
    body = r.text
    assert "Roof" in body
    assert "4.25" in body
    assert "31.30" in body
    assert "839.0" in body
    assert "78" in body
    # HTMX polling wired up
    assert 'hx-get="/tiles"' in body
    assert 'hx-trigger="every 30s"' in body


async def test_tiles_fragment_returns_just_the_tile_html(
    tiles_client: tuple[httpx.AsyncClient, StubTilesService],
) -> None:
    ac, stub = tiles_client
    r = await ac.get("/tiles")
    assert r.status_code == 200
    body = r.text
    assert "Current power" in body
    assert "4.25" in body
    # No layout — fragment shouldn't carry the base shell
    assert "<html" not in body.lower()
    assert "<body" not in body.lower()
    assert stub.get_calls == ["S1"]


async def test_tiles_endpoint_requires_authentication() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        r = await ac.get("/tiles", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.parametrize(
    "tiles_client",
    [StubTilesService(default_id=None, tiles=None)],
    indirect=True,
)
async def test_home_renders_friendly_message_when_no_station(
    tiles_client: tuple[httpx.AsyncClient, StubTilesService],
) -> None:
    ac, _ = tiles_client
    r = await ac.get("/")
    assert r.status_code == 200
    assert "No stations found" in r.text


@pytest.mark.parametrize(
    "tiles_client",
    [StubTilesService(get_error=SolisAPIError("1004", "rate limited"))],
    indirect=True,
)
async def test_home_surfaces_solis_error_inline(
    tiles_client: tuple[httpx.AsyncClient, StubTilesService],
) -> None:
    ac, _ = tiles_client
    r = await ac.get("/")
    assert r.status_code == 200
    assert "SolisCloud rejected the call" in r.text
    assert "1004" in r.text


@pytest.mark.parametrize(
    "tiles_client",
    [
        StubTilesService(
            tiles=_sample_tiles(
                stale=True,
                error="rate limited (1004)",
                current_power=2.5,
            )
        )
    ],
    indirect=True,
)
async def test_home_shows_stale_badge_when_falling_back_from_db(
    tiles_client: tuple[httpx.AsyncClient, StubTilesService],
) -> None:
    ac, _ = tiles_client
    r = await ac.get("/")
    assert r.status_code == 200
    assert "Showing last-known values" in r.text
    assert "rate limited (1004)" in r.text


@pytest.mark.parametrize(
    "tiles_client",
    [
        StubTilesService(
            default_error=RuntimeError("SOLIS_KEY_ID / SOLIS_KEYSECRET not configured")
        )
    ],
    indirect=True,
)
async def test_home_shows_friendly_error_when_solis_unconfigured(
    tiles_client: tuple[httpx.AsyncClient, StubTilesService],
) -> None:
    ac, _ = tiles_client
    r = await ac.get("/")
    assert r.status_code == 200
    assert "Could not reach SolisCloud" in r.text
