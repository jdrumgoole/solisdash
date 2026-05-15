"""Regression test for the v0.6.0 "Internal Server Error on signin" bug.

`get_solis_client` used to raise `RuntimeError("SOLIS_KEY_ID / SOLIS_KEYSECRET
not configured")` when those settings were empty. Because that raise happened
inside FastAPI's dependency resolution for the home page (`Depends(get_tiles_service)`
→ `Depends(get_solis_client)`), every protected page returned a 500 right after
the user finished the setup wizard with the SolisCloud key blank — `_resolve_tiles`
never got a chance to render the friendly alert.

Now `get_solis_client` builds the client with whatever creds it has; SolisCloud
rejects the call and `_resolve_tiles` surfaces it as
"SolisCloud rejected the call: …".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.app import app, get_db, get_solis_client
from solisdash.auth import create_user
from solisdash.client import SolisClient
from solisdash.config import get_settings


async def test_get_solis_client_does_not_raise_on_empty_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The minimal lock-in for the fix — direct unit call must not raise."""
    monkeypatch.setenv("SOLIS_KEY_ID", "")
    monkeypatch.setenv("SOLIS_KEYSECRET", "")
    get_settings.cache_clear()

    fake_state = SimpleNamespace(solis_client=None)
    fake_app = SimpleNamespace(state=fake_state)
    fake_request = SimpleNamespace(app=fake_app)

    client = await get_solis_client(fake_request)  # type: ignore[arg-type]
    assert isinstance(client, SolisClient)
    get_settings.cache_clear()


async def test_signed_in_home_renders_alert_when_solis_creds_empty(
    clean_db: AsyncDatabase[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: signin → GET / must be 200 with a friendly alert."""

    def _solis_rejects(request: httpx.Request) -> httpx.Response:
        # What SolisCloud actually returns for empty / wrongly-signed calls.
        return httpx.Response(
            403,
            json={
                "timestamp": 0,
                "status": 403,
                "error": "Forbidden",
                "message": "wrong sign",
                "path": request.url.path,
            },
        )

    async def _override_db() -> AsyncDatabase[dict[str, Any]]:
        return clean_db

    async def _override_solis() -> SolisClient:
        return SolisClient(
            base_url="http://test",
            key_id="",
            key_secret="",
            transport=httpx.MockTransport(_solis_rejects),
            max_retries=0,
        )

    # Reset shared state so the previous test's tiles_service / mongo_client
    # don't leak. ASGITransport doesn't run the lifespan, so we seed the
    # slots the real code expects.
    app.state.mongo_client = None
    app.state.solis_client = None
    app.state.tiles_service = None

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_solis_client] = _override_solis
    # Importantly: do NOT override get_tiles_service. We want the real
    # composition (`get_tiles_service` → `get_solis_client`) to run.
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
            assert r.headers["location"] == "/"

            # The actual reported click: hitting "/" after signin must not 500.
            r = await ac.get("/")
        assert r.status_code == 200, f"home returned {r.status_code}"
        # The friendly-error path renders one of two phrasings.
        assert (
            "SolisCloud rejected the call" in r.text
            or "Could not reach SolisCloud" in r.text
        ), "expected a friendly alert about SolisCloud, got something else"
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_solis_client, None)
        # Reset for the next test in the same worker.
        app.state.tiles_service = None
        app.state.solis_client = None
        app.state.mongo_client = None
