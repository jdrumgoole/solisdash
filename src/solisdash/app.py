"""Solisdash FastAPI app: session auth, Jinja shell, lazy-connected Mongo."""

from __future__ import annotations

import base64
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError
from starlette.middleware.sessions import SessionMiddleware

from solisdash import __version__
from solisdash.alarms import ALARM_STATE_LABELS, AlarmService
from solisdash.auth import (
    authenticate,
    create_user,
    get_current_user,
    redirect_to,
    require_user,
    session_login,
    session_logout,
)
from solisdash.client import SolisAPIError, SolisClient
from solisdash.config import get_settings
from solisdash.configfile import delete_toml, user_config_toml_path, write_toml
from solisdash.db import ensure_indexes
from solisdash.history import HistoryService, Series, parse_month
from solisdash.poller import Poller
from solisdash.ratelimit import TokenBucket
from solisdash.scheduler import build_scheduler
from solisdash.tiles import LiveTilesService, TilesData

# Uvicorn only configures its own named loggers, leaving the root logger
# without handlers. `basicConfig` here gives `solisdash.*` loggers a default
# stderr handler at INFO so the scheduler/poller logs land in `uvicorn.log`.
# `basicConfig` is a no-op if the root logger already has handlers, so this
# stays out of the way when uvicorn (or pytest) has wired up something else.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

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
    """Lazily open one shared `SolisClient`. Closed on app shutdown.

    The client is built even with empty key/secret so a freshly-installed
    instance with no SolisCloud configuration doesn't 500 on every
    protected page. Empty creds cause SolisCloud to reject the call,
    which `_resolve_tiles` catches and renders as a friendly alert
    ("SolisCloud rejected the call: …" — visible nudge to configure).
    """
    existing: SolisClient | None = getattr(request.app.state, "solis_client", None)
    if existing is None:
        settings = get_settings()
        existing = SolisClient(
            base_url=settings.SOLIS_API_URL,
            key_id=settings.SOLIS_KEY_ID,
            key_secret=settings.SOLIS_KEYSECRET,
        )
        request.app.state.solis_client = existing
    return existing


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


async def get_alarm_service(
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
) -> AlarmService:
    return AlarmService(db)


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


@app.get("/ready")
async def ready(
    request: Request,
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
) -> JSONResponse:
    """Readiness probe: Mongo reachable, scheduler healthy (if enabled).

    Always returns JSON. 200 when all checks pass, 503 otherwise. Liveness
    (`/health`) only proves the process is alive; this proves the app can
    actually serve work.
    """
    settings = get_settings()
    checks: dict[str, Any] = {}
    ok = True

    try:
        await db.command("ping")
        checks["mongo"] = {"ok": True}
    except Exception as exc:
        checks["mongo"] = {"ok": False, "detail": str(exc)}
        ok = False

    if settings.RUN_SCHEDULER:
        scheduler = request.app.state.scheduler
        if scheduler is None or not scheduler.running:
            checks["scheduler"] = {"ok": False, "detail": "not running"}
            ok = False
        else:
            try:
                latest = await db["station_samples"].find_one(
                    sort=[("polled_at", -1)]
                )
            except Exception as exc:
                checks["scheduler"] = {"ok": False, "detail": str(exc)}
                ok = False
            else:
                if latest is None:
                    checks["scheduler"] = {
                        "ok": False,
                        "detail": "no station_samples yet",
                    }
                    ok = False
                else:
                    polled_at = latest.get("polled_at")
                    if isinstance(polled_at, datetime):
                        # Mongo's BSON dates come back naive (timezone.utc) by default.
                        if polled_at.tzinfo is None:
                            polled_at = polled_at.replace(tzinfo=timezone.utc)
                        age_s: float | None = (
                            datetime.now(timezone.utc) - polled_at
                        ).total_seconds()
                    else:
                        age_s = None
                    stale_after = settings.SCHEDULER_SAMPLE_MINUTES * 60 * 3
                    healthy = age_s is not None and age_s < stale_after
                    checks["scheduler"] = {
                        "ok": healthy,
                        "last_sample_age_s": age_s,
                        "stale_after_s": stale_after,
                    }
                    if not healthy:
                        ok = False
    else:
        checks["scheduler"] = {"ok": True, "detail": "RUN_SCHEDULER disabled"}

    return JSONResponse(
        {"ready": ok, "version": __version__, "checks": checks},
        status_code=200 if ok else 503,
    )


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
    """Fetch the default station's tiles. Return (data, error_message).

    Catches the expected failure modes (SolisCloud envelope errors, httpx
    transport errors, and `RuntimeError` from unconfigured settings) and
    renders them as friendly alerts. Anything else propagates so genuine
    bugs surface in logs.
    """
    try:
        station_id = await tiles_service.default_station_id()
    except SolisAPIError as exc:
        return None, f"SolisCloud rejected the call: {exc}"
    except (httpx.HTTPError, RuntimeError) as exc:
        return None, f"Could not reach SolisCloud: {exc}"
    if not station_id:
        return None, "No stations found on this SolisCloud account."
    try:
        return await tiles_service.get_tiles(station_id), None
    except SolisAPIError as exc:
        return None, f"SolisCloud rejected the call: {exc}"
    except (httpx.HTTPError, RuntimeError) as exc:
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


async def _users_exist_in(uri: str, db_name: str) -> bool:
    """Count documents in `users` for an arbitrary URI. Returns False on failure."""
    client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(uri, serverSelectionTimeoutMS=2000)
    try:
        return await client[db_name]["users"].count_documents({}, limit=1) > 0
    except Exception:
        return False
    finally:
        await client.close()


async def _setup_done() -> bool:
    """True once Mongo is configured AND there's at least one user."""
    settings = get_settings()
    if not settings.SOLIS_MONGODB_URI:
        return False
    return await _users_exist_in(settings.SOLIS_MONGODB_URI, settings.SOLIS_MONGODB_DB)


def _setup_defaults() -> dict[str, str]:
    """Current settings rendered as form defaults for the wizard / settings page."""
    s = get_settings()
    return {
        "SOLIS_MONGODB_URI": s.SOLIS_MONGODB_URI or "mongodb://localhost:27017/",
        "SOLIS_MONGODB_DB": s.SOLIS_MONGODB_DB or "solis",
        "SOLIS_API_URL": s.SOLIS_API_URL,
        "SOLIS_KEY_ID": s.SOLIS_KEY_ID,
        # Never echo the secret back to the page — admin re-enters if changing.
        "SOLIS_KEYSECRET_PRESENT": "yes" if s.SOLIS_KEYSECRET else "",
        "SOLIS_STATION_ID": s.SOLIS_STATION_ID,
    }


async def _invalidate_clients(app: FastAPI) -> None:
    """Close cached Mongo / SolisCloud clients so the next request re-creates
    them against any settings we just persisted."""
    mongo: AsyncMongoClient[dict[str, Any]] | None = getattr(
        app.state, "mongo_client", None
    )
    if mongo is not None:
        await mongo.close()
    solis: SolisClient | None = getattr(app.state, "solis_client", None)
    if solis is not None:
        await solis.aclose()
    app.state.mongo_client = None
    app.state.solis_client = None
    app.state.tiles_service = None


@app.get("/login", response_class=HTMLResponse, response_model=None)
async def login_form(
    request: Request,
    user: dict[str, Any] | None = Depends(get_current_user),
) -> HTMLResponse | RedirectResponse:
    if user is not None:
        return redirect_to("/")
    if not await _setup_done():
        return redirect_to("/setup")
    return templates.TemplateResponse(request, "login.html", {"error": None})


# --- First-run setup wizard ------------------------------------------------


@app.get("/setup", response_class=HTMLResponse, response_model=None)
async def setup_form(
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """First-run wizard: MongoDB + SolisCloud + admin account on one page."""
    if await _setup_done():
        return redirect_to("/login")
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"error": None, "username": "", "defaults": _setup_defaults()},
    )


@app.post("/setup/test/mongo", response_class=HTMLResponse, response_model=None)
async def setup_test_mongo(
    mongo_uri: str = Form(...),
    mongo_db: str = Form("solis"),
) -> HTMLResponse:
    """HTMX endpoint — try to connect to the URI the form is currently holding."""
    try:
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            mongo_uri, serverSelectionTimeoutMS=4000
        )
        try:
            await client[mongo_db].command("ping")
        finally:
            await client.close()
    except Exception as exc:
        return HTMLResponse(
            f'<p class="test-result error">✗ MongoDB connection failed: '
            f"{type(exc).__name__}: {exc}</p>",
            status_code=200,
        )
    return HTMLResponse(
        f'<p class="test-result success">✓ Connected to MongoDB at '
        f"<code>{mongo_uri}</code>.</p>"
    )


@app.post("/setup/test/soliscloud", response_class=HTMLResponse, response_model=None)
async def setup_test_soliscloud(
    solis_api_url: str = Form(...),
    solis_key_id: str = Form(""),
    solis_keysecret: str = Form(""),
) -> HTMLResponse:
    """HTMX endpoint — sign a probe `userStationList` call with the supplied creds."""
    if not solis_key_id or not solis_keysecret:
        return HTMLResponse(
            '<p class="test-result error">✗ Enter both Key ID and Key Secret '
            "before testing.</p>",
            status_code=200,
        )
    try:
        async with SolisClient(
            base_url=solis_api_url,
            key_id=solis_key_id,
            key_secret=solis_keysecret,
            max_retries=0,
        ) as c:
            page = await c.user_station_list(page_no=1, page_size=1)
    except SolisAPIError as exc:
        return HTMLResponse(
            f'<p class="test-result error">✗ SolisCloud rejected the call: '
            f"[{exc.code}] {exc.msg}</p>"
        )
    except Exception as exc:
        return HTMLResponse(
            f'<p class="test-result error">✗ Could not reach SolisCloud: '
            f"{type(exc).__name__}: {exc}</p>"
        )
    return HTMLResponse(
        f'<p class="test-result success">✓ SolisCloud accepted the credentials '
        f"({page.total} station(s) on the account).</p>"
    )


@app.post("/setup", response_class=HTMLResponse, response_model=None)
async def setup_submit(
    request: Request,
    mongo_uri: str = Form(...),
    mongo_db: str = Form("solis"),
    solis_api_url: str = Form(""),
    solis_key_id: str = Form(""),
    solis_keysecret: str = Form(""),
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """Validate, persist to toml, create the first admin, sign in."""
    import secrets

    def _err(msg: str, **extras: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "error": msg,
                "username": username,
                "defaults": {**_setup_defaults(), **extras},
            },
            status_code=400,
        )

    if await _setup_done():
        return redirect_to("/login")
    if not mongo_uri.strip():
        return _err("MongoDB URI is required.")
    if not username.strip():
        return _err("Username must not be empty.")
    if password != confirm:
        return _err("Passwords do not match.")
    if not password:
        return _err("Password must not be empty.")

    target_uri = mongo_uri.strip()
    target_db = mongo_db.strip() or "solis"
    target_username = username.strip()

    # Verify the supplied URI works AND check for a username collision
    # *before* we persist anything. Without this guard, pointing the wizard
    # at an already-populated database (e.g. recovering after losing the
    # local toml) raises DuplicateKeyError as a 500 once we hit the index.
    try:
        probe: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            target_uri, serverSelectionTimeoutMS=4000
        )
        try:
            probe_db = probe[target_db]
            await probe_db.command("ping")
            collision = await probe_db["users"].find_one(
                {"username": target_username}, projection={"_id": 1}
            )
        finally:
            await probe.close()
    except Exception as exc:
        return _err(f"MongoDB connection failed: {type(exc).__name__}: {exc}")

    if collision is not None:
        return _err(
            f"A user named “{target_username}” already exists in this database. "
            "Pick a different username, or sign in to the existing account "
            "from the /login page after saving."
        )

    # Persist everything that's non-empty.
    settings = get_settings()
    write_toml(
        {
            "SOLIS_MONGODB_URI": target_uri,
            "SOLIS_MONGODB_DB": target_db,
            "SOLIS_API_URL": solis_api_url.strip() or settings.SOLIS_API_URL,
            "SOLIS_KEY_ID": solis_key_id.strip(),
            "SOLIS_KEYSECRET": solis_keysecret,
            "SESSION_SECRET": settings.SESSION_SECRET or secrets.token_urlsafe(32),
        }
    )
    get_settings.cache_clear()
    await _invalidate_clients(request.app)

    # Create the first admin in the freshly-configured Mongo.
    db = await get_db(request)
    await ensure_indexes(db)
    try:
        await create_user(db, username=target_username, password=password, role="admin")
    except DuplicateKeyError:
        # Pre-check above usually catches this; the index also enforces it,
        # so handle the race anyway rather than 500ing.
        return _err(
            f"A user named “{target_username}” already exists in this database. "
            "Pick a different username, or sign in to the existing account "
            "from the /login page."
        )

    # Auto-sign-in — saves the user re-typing the password they just chose.
    session_login(request, {"username": target_username, "role": "admin"})
    return redirect_to("/")


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


# --- Settings page ---------------------------------------------------------


@app.get("/settings", response_class=HTMLResponse, response_model=None)
async def settings_page(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "config_path": str(user_config_toml_path()),
            "defaults": _setup_defaults(),
            "error": None,
            "saved": False,
        },
    )


@app.post("/settings/save", response_class=HTMLResponse, response_model=None)
async def settings_save(
    request: Request,
    mongo_uri: str = Form(...),
    mongo_db: str = Form("solis"),
    solis_api_url: str = Form(""),
    solis_key_id: str = Form(""),
    solis_keysecret: str = Form(""),
    solis_station_id: str = Form(""),
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse | RedirectResponse:
    """Persist edited settings to `solisdash.toml`, invalidate cached clients."""
    settings = get_settings()
    updates: dict[str, Any] = {
        "SOLIS_MONGODB_URI": mongo_uri.strip(),
        "SOLIS_MONGODB_DB": mongo_db.strip() or "solis",
        "SOLIS_API_URL": solis_api_url.strip() or settings.SOLIS_API_URL,
        "SOLIS_KEY_ID": solis_key_id.strip(),
        "SOLIS_STATION_ID": solis_station_id.strip(),
    }
    # Only overwrite the secret if the user actually entered a new one.
    if solis_keysecret:
        updates["SOLIS_KEYSECRET"] = solis_keysecret

    write_toml(updates)
    get_settings.cache_clear()
    await _invalidate_clients(request.app)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "config_path": str(user_config_toml_path()),
            "defaults": _setup_defaults(),
            "error": None,
            "saved": True,
        },
    )


@app.post("/settings/test/mongo", response_class=HTMLResponse, response_model=None)
async def settings_test_mongo(
    mongo_uri: str = Form(...),
    mongo_db: str = Form("solis"),
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    return await setup_test_mongo(mongo_uri=mongo_uri, mongo_db=mongo_db)


@app.post("/settings/test/soliscloud", response_class=HTMLResponse, response_model=None)
async def settings_test_soliscloud(
    solis_api_url: str = Form(...),
    solis_key_id: str = Form(""),
    solis_keysecret: str = Form(""),
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    return await setup_test_soliscloud(
        solis_api_url=solis_api_url,
        solis_key_id=solis_key_id,
        solis_keysecret=solis_keysecret,
    )


@app.post("/settings/reset", response_model=None)
async def settings_reset(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
) -> RedirectResponse:
    """Wipe `solisdash.toml`, clear cached clients + session, send the user
    back through the first-run wizard."""
    delete_toml()
    get_settings.cache_clear()
    await _invalidate_clients(request.app)
    session_logout(request)
    return redirect_to("/setup")


# --- History page -----------------------------------------------------------


@app.get("/history", response_class=HTMLResponse, response_model=None)
async def history_page(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    history: HistoryService = Depends(get_history_service),
) -> HTMLResponse:
    stations = await history.list_stations()
    selected = stations[0]["id"] if stations else None
    today = datetime.now(timezone.utc).date().isoformat()
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


@app.post("/history/poll-now", response_class=HTMLResponse, response_model=None)
async def history_poll_now(
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
    solis: SolisClient = Depends(get_solis_client),
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    """Pull `stationDetail` for every station once and upsert the snapshots.

    Lets the user populate `stations` / `station_samples` from the GUI on a
    fresh install (or recover after the scheduler has been off), instead of
    having to drop to a shell and run `invoke poll-once`.
    """
    poller = Poller(solis=solis, db=db)
    try:
        wrote = await poller.poll_current_all()
    except SolisAPIError as exc:
        return HTMLResponse(
            f'<p role="alert" class="error">SolisCloud rejected the call: '
            f"[{exc.code}] {exc.msg}. Check the SolisCloud credentials in "
            f'<a href="/settings">Settings</a>.</p>',
            status_code=200,
        )
    except httpx.HTTPError as exc:
        return HTMLResponse(
            f'<p role="alert" class="error">Could not reach SolisCloud: '
            f"{type(exc).__name__}: {exc}</p>",
            status_code=200,
        )
    if wrote == 0:
        return HTMLResponse(
            '<p role="alert" class="error">SolisCloud returned no stations for '
            'this account. Check the API key in <a href="/settings">Settings</a>.</p>'
        )
    # Trigger a full page reload via HTMX so the chart UI mounts in place of
    # the empty-state alert.
    return HTMLResponse(
        f'<p class="test-result success">✓ Polled {wrote} station(s). '
        "Reloading…</p>",
        headers={"HX-Refresh": "true"},
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


def _series_to_csv(series: Series, *, x_header: str, y_header: str) -> str:
    """Render a Series as a tiny RFC-4180 CSV."""
    lines = [f"{x_header},{y_header} ({series.unit})"]
    for p in series.points:
        v = "" if p.v is None else f"{p.v}"
        lines.append(f"{p.t},{v}")
    return "\n".join(lines) + "\n"


def _csv_response(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/history/day.csv")
async def history_day_csv(
    station_id: str | None = Query(None),
    when: str = Query(..., description="YYYY-MM-DD"),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response("timestamp_ms,power\n", "history-day.csv")
    try:
        when_date = date.fromisoformat(when)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.day_series(sid, when_date)
    return _csv_response(
        _series_to_csv(series, x_header="timestamp_ms", y_header="power"),
        f"history-day-{sid}-{when}.csv",
    )


@app.get("/history/month.csv")
async def history_month_csv(
    station_id: str | None = Query(None),
    month: str = Query(..., description="YYYY-MM"),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response("date,energy\n", "history-month.csv")
    try:
        parse_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.month_daily(sid, month)
    return _csv_response(
        _series_to_csv(series, x_header="date", y_header="energy"),
        f"history-month-{sid}-{month}.csv",
    )


@app.get("/history/year.csv")
async def history_year_csv(
    station_id: str | None = Query(None),
    year: int = Query(...),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response("month,energy\n", "history-year.csv")
    series = await history.year_monthly(sid, year)
    return _csv_response(
        _series_to_csv(series, x_header="month", y_header="energy"),
        f"history-year-{sid}-{year}.csv",
    )


@app.get("/history/all.csv")
async def history_all_csv(
    station_id: str | None = Query(None),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response("year,energy\n", "history-all.csv")
    series = await history.all_time(sid)
    return _csv_response(
        _series_to_csv(series, x_header="year", y_header="energy"),
        f"history-all-{sid}.csv",
    )


# --- Alarms page ------------------------------------------------------------


@app.get("/alarms", response_class=HTMLResponse, response_model=None)
async def alarms_page(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    history: HistoryService = Depends(get_history_service),
    alarms_service: AlarmService = Depends(get_alarm_service),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    station_id: str | None = Query(None),
    state: str | None = Query(None),
) -> HTMLResponse:
    stations = await history.list_stations()
    page_data = await alarms_service.list_alarms(
        page_no=page,
        page_size=page_size,
        station_id=station_id or None,
        state=state or None,
    )
    return templates.TemplateResponse(
        request,
        "alarms.html",
        {
            "user": user,
            "stations": stations,
            "selected_station_id": station_id or "",
            "selected_state": state or "",
            "state_labels": ALARM_STATE_LABELS,
            "alarms": page_data,
        },
    )
