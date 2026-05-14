"""Solisdash FastAPI app: session auth, Jinja shell, lazy-connected Mongo."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from starlette.middleware.sessions import SessionMiddleware

from solisdash import __version__
from solisdash.auth import (
    authenticate,
    get_current_user,
    redirect_to,
    require_user,
    session_login,
    session_logout,
)
from solisdash.client import SolisAPIError, SolisClient
from solisdash.config import get_settings
from solisdash.db import ensure_indexes
from solisdash.history import HistoryService, parse_month
from solisdash.poller import Poller
from solisdash.ratelimit import TokenBucket
from solisdash.scheduler import build_scheduler
from solisdash.tiles import LiveTilesService, TilesData

HERE = Path(__file__).parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

# 1x1 transparent PNG, just to silence /favicon.ico 404s during dev.
_FAVICON_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["version"] = __version__


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise lazy-init slots; close everything on shutdown.

    When `RUN_SCHEDULER` is set, also start an `AsyncIOScheduler` that
    pumps SolisCloud data into MongoDB on a cron. Tests and CI default
    to off so they never call out to the real API.
    """
    app.state.mongo_client = None
    app.state.solis_client = None
    app.state.tiles_service = None
    app.state.scheduler = None
    app.state.poller = None
    app.state.rate_limiter = None

    settings = get_settings()
    if settings.RUN_SCHEDULER and settings.SOLIS_KEY_ID and settings.SOLIS_MONGODB_URI:
        app.state.rate_limiter = TokenBucket(
            rate=settings.SCHEDULER_RATE_PER_SEC,
            capacity=settings.SCHEDULER_RATE_PER_SEC * 2,
        )
        solis = SolisClient(
            base_url=settings.SOLIS_API_URL,
            key_id=settings.SOLIS_KEY_ID,
            key_secret=settings.SOLIS_KEYSECRET,
        )
        mongo: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            settings.SOLIS_MONGODB_URI
        )
        await ensure_indexes(mongo[settings.SOLIS_MONGODB_DB])
        app.state.solis_client = solis
        app.state.mongo_client = mongo
        app.state.poller = Poller(
            solis=solis,
            db=mongo[settings.SOLIS_MONGODB_DB],
            rate_limiter=app.state.rate_limiter,
        )
        app.state.scheduler = build_scheduler(app.state.poller, settings)
        app.state.scheduler.start()

    try:
        yield
    finally:
        scheduler = app.state.scheduler
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        mongo_cli: AsyncMongoClient[dict[str, Any]] | None = app.state.mongo_client
        if mongo_cli is not None:
            await mongo_cli.close()
        solis_cli: SolisClient | None = app.state.solis_client
        if solis_cli is not None:
            await solis_cli.aclose()


app = FastAPI(title="Solisdash", version=__version__, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().SESSION_SECRET or "dev-only-do-not-use-in-prod",
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


async def get_db(request: Request) -> AsyncDatabase[dict[str, Any]]:
    """Lazily open the Mongo client on first use; reuse for the app lifetime."""
    settings = get_settings()
    if request.app.state.mongo_client is None:
        if not settings.SOLIS_MONGODB_URI:
            raise RuntimeError("SOLIS_MONGODB_URI is not configured")
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            settings.SOLIS_MONGODB_URI
        )
        await ensure_indexes(client[settings.SOLIS_MONGODB_DB])
        request.app.state.mongo_client = client
    cached: AsyncMongoClient[dict[str, Any]] = request.app.state.mongo_client
    return cached[settings.SOLIS_MONGODB_DB]


async def get_solis_client(request: Request) -> SolisClient:
    """Lazily open one shared `SolisClient`. Closed on app shutdown."""
    if request.app.state.solis_client is None:
        settings = get_settings()
        if not settings.SOLIS_KEY_ID or not settings.SOLIS_KEYSECRET:
            raise RuntimeError("SOLIS_KEY_ID / SOLIS_KEYSECRET not configured")
        request.app.state.solis_client = SolisClient(
            base_url=settings.SOLIS_API_URL,
            key_id=settings.SOLIS_KEY_ID,
            key_secret=settings.SOLIS_KEYSECRET,
        )
    client: SolisClient = request.app.state.solis_client
    return client


async def get_tiles_service(
    request: Request,
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
    solis: SolisClient = Depends(get_solis_client),
) -> LiveTilesService:
    """Single `LiveTilesService` per app, so the in-memory TTL cache persists."""
    if request.app.state.tiles_service is None:
        settings = get_settings()
        request.app.state.tiles_service = LiveTilesService(
            solis=solis,
            db=db,
            default_station_id=settings.SOLIS_STATION_ID or None,
        )
    service: LiveTilesService = request.app.state.tiles_service
    return service


async def get_history_service(
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
) -> HistoryService:
    """A fresh HistoryService is cheap; no in-memory state to share."""
    return HistoryService(db)


async def _resolve_station_id(
    history: HistoryService, requested: str | None
) -> str | None:
    """Use `requested` if given, else fall back to the first stored station."""
    if requested:
        return requested
    stations = await history.list_stations()
    return stations[0]["id"] if stations else None


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Empty 1x1 transparent PNG so browsers stop 404-ing during dev."""
    return Response(
        content=_FAVICON_PNG,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _resolve_tiles(
    tiles_service: LiveTilesService,
) -> tuple[TilesData | None, str | None]:
    """Fetch the default station's tiles. Return (data, error_message)."""
    try:
        station_id = await tiles_service.default_station_id()
    except SolisAPIError as exc:
        return None, f"SolisCloud rejected the call: {exc}"
    except Exception as exc:
        return None, f"Could not reach SolisCloud: {exc}"
    if not station_id:
        return None, "No stations found on this SolisCloud account."
    try:
        return await tiles_service.get_tiles(station_id), None
    except SolisAPIError as exc:
        return None, f"SolisCloud rejected the call: {exc}"
    except Exception as exc:
        return None, f"Could not reach SolisCloud: {exc}"


@app.get("/", response_class=HTMLResponse, response_model=None)
async def home(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    tiles_service: LiveTilesService = Depends(get_tiles_service),
) -> HTMLResponse:
    tiles, error = await _resolve_tiles(tiles_service)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"user": user, "tiles": tiles, "error": error},
    )


@app.get("/tiles", response_class=HTMLResponse, response_model=None)
async def tiles_fragment(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    tiles_service: LiveTilesService = Depends(get_tiles_service),
) -> HTMLResponse:
    """HTML fragment for HTMX swaps on the home page."""
    tiles, error = await _resolve_tiles(tiles_service)
    return templates.TemplateResponse(
        request, "_tiles.html", {"tiles": tiles, "error": error}
    )


@app.get("/login", response_class=HTMLResponse, response_model=None)
async def login_form(
    request: Request,
    user: dict[str, Any] | None = Depends(get_current_user),
) -> HTMLResponse | RedirectResponse:
    if user is not None:
        return redirect_to("/")
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    user = await authenticate(db, username, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password.", "username": username},
            status_code=401,
        )
    session_login(request, user)
    return redirect_to("/")


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    session_logout(request)
    return redirect_to("/login")


# --- History page -----------------------------------------------------------


@app.get("/history", response_class=HTMLResponse, response_model=None)
async def history_page(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    history: HistoryService = Depends(get_history_service),
) -> HTMLResponse:
    stations = await history.list_stations()
    selected = stations[0]["id"] if stations else None
    today = datetime.now(UTC).date().isoformat()
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "user": user,
            "stations": stations,
            "selected_station_id": selected,
            "today": today,
            "current_month": today[:7],
            "current_year": today[:4],
        },
    )


@app.get("/history/day.json")
async def history_day_json(
    station_id: str | None = Query(None),
    when: str = Query(..., description="YYYY-MM-DD"),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return JSONResponse(_empty_series_response("Power", "kW", station_id=None))
    try:
        when_date = date.fromisoformat(when)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.day_series(sid, when_date)
    return JSONResponse({"station_id": sid, **series.to_json()})


@app.get("/history/month.json")
async def history_month_json(
    station_id: str | None = Query(None),
    month: str = Query(..., description="YYYY-MM"),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return JSONResponse(_empty_series_response("Daily energy", "kWh", station_id=None))
    try:
        parse_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.month_daily(sid, month)
    return JSONResponse({"station_id": sid, **series.to_json()})


@app.get("/history/year.json")
async def history_year_json(
    station_id: str | None = Query(None),
    year: int = Query(...),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return JSONResponse(
            _empty_series_response("Monthly energy", "kWh", station_id=None)
        )
    series = await history.year_monthly(sid, year)
    return JSONResponse({"station_id": sid, **series.to_json()})


@app.get("/history/all.json")
async def history_all_json(
    station_id: str | None = Query(None),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return JSONResponse(
            _empty_series_response("Annual energy", "kWh", station_id=None)
        )
    series = await history.all_time(sid)
    return JSONResponse({"station_id": sid, **series.to_json()})


def _empty_series_response(
    label: str, unit: str, *, station_id: str | None
) -> dict[str, Any]:
    return {"station_id": station_id, "label": label, "unit": unit, "points": []}
