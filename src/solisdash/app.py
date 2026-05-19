"""Solisdash FastAPI app: session auth, Jinja shell, lazy-connected Mongo."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
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
from solisdash.db import SOLISCLOUD_COLLECTIONS, ensure_indexes
from solisdash.history import (
    METRIC_ENERGY,
    METRIC_POWER,
    METRIC_SUPPORTS,
    VIEW_ALL,
    VIEW_DAY,
    VIEW_MONTH,
    VIEW_YEAR,
    HistoryService,
    Series,
    metric_supports,
    parse_month,
)
from solisdash.poller import Poller, iter_dates, iter_months
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

_FAVICON_PNG = (HERE / "static" / "favicon.png").read_bytes()

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["version"] = __version__
# Per-process cache buster for static assets. Changes on every server
# restart so a CSS / JS edit invalidates the browser cache as soon as
# the new process is up. Released builds get a fresh nonce because they
# start a fresh process; in dev, `invoke restart` is enough to bust.
import time as _time  # noqa: E402

templates.env.globals["asset_version"] = f"{__version__}-{_time.time_ns()}"


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

    await _ensure_scheduler_running(app)

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
    """Serve the bundled Solisdash icon as the favicon. Same file the
    pywebview window uses as its dock icon. Browsers cope with PNG
    content at the `.ico` URL fine."""
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


def _setup_defaults() -> dict[str, Any]:
    """Current settings rendered as form defaults for the wizard / settings page."""
    s = get_settings()
    return {
        "SOLIS_MONGODB_URI": s.SOLIS_MONGODB_URI or "mongodb://localhost:27017/",
        "SOLIS_MONGODB_DB": s.SOLIS_MONGODB_DB or "solis",
        "SOLIS_API_URL": s.SOLIS_API_URL,
        "SOLIS_KEY_ID": s.SOLIS_KEY_ID,
        # Single-user dashboard behind login — the user wants to see what's
        # configured, so we render the actual secret value rather than
        # masking it. Anyone with login access can already read
        # `solisdash.toml` on disk anyway.
        "SOLIS_KEYSECRET": s.SOLIS_KEYSECRET,
        "SOLIS_STATION_ID": s.SOLIS_STATION_ID,
        "SOLIS_FEED_IN_TARIFF": s.SOLIS_FEED_IN_TARIFF,
        "SOLIS_IMPORT_TARIFF": s.SOLIS_IMPORT_TARIFF,
        "SOLIS_CURRENCY": s.SOLIS_CURRENCY,
        "RUN_SCHEDULER": s.RUN_SCHEDULER,
        "SCHEDULER_SAMPLE_MINUTES": s.SCHEDULER_SAMPLE_MINUTES,
        "SCHEDULER_DAILY_HOUR_UTC": s.SCHEDULER_DAILY_HOUR_UTC,
        "SCHEDULER_DAILY_MINUTE_UTC": s.SCHEDULER_DAILY_MINUTE_UTC,
        "SCHEDULER_RATE_PER_SEC": s.SCHEDULER_RATE_PER_SEC,
    }


async def _ensure_scheduler_running(app: FastAPI) -> None:
    """Start the background poller if config is complete and it isn't already.

    Called from `lifespan` at boot AND from the wizard / settings-save paths,
    so that turning on `RUN_SCHEDULER` (or wiring up Mongo + SolisCloud for
    the first time) starts the cron immediately rather than waiting for a
    process restart. Idempotent — a no-op when the scheduler is already
    running or the config still isn't complete enough.
    """
    settings = get_settings()
    needs_start = (
        settings.RUN_SCHEDULER
        and settings.SOLIS_KEY_ID
        and settings.SOLIS_MONGODB_URI
        and getattr(app.state, "scheduler", None) is None
    )
    if not needs_start:
        return
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


async def _invalidate_clients(app: FastAPI) -> None:
    """Close cached Mongo / SolisCloud clients AND stop the scheduler so the
    next request re-creates them against any settings we just persisted."""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        app.state.scheduler = None
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
    app.state.poller = None
    app.state.rate_limiter = None


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

    # Persist everything that's non-empty. RUN_SCHEDULER defaults to True
    # after wizard completion — a self-hosted dashboard wants the daily
    # rollup cron running. Dev/CI never goes through the wizard, so their
    # `Settings.RUN_SCHEDULER` default of `False` is preserved.
    settings = get_settings()
    write_toml(
        {
            "SOLIS_MONGODB_URI": target_uri,
            "SOLIS_MONGODB_DB": target_db,
            "SOLIS_API_URL": solis_api_url.strip() or settings.SOLIS_API_URL,
            "SOLIS_KEY_ID": solis_key_id.strip(),
            "SOLIS_KEYSECRET": solis_keysecret,
            "SESSION_SECRET": settings.SESSION_SECRET or secrets.token_urlsafe(32),
            "RUN_SCHEDULER": True,
        }
    )
    get_settings.cache_clear()
    await _invalidate_clients(request.app)
    await _ensure_scheduler_running(request.app)

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
            "purged_counts": None,
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
    feed_in_tariff: float = Form(0.0),
    import_tariff: float = Form(0.0),
    currency: str = Form("EUR"),
    run_scheduler: str | None = Form(None),
    sample_minutes: int = Form(5),
    daily_hour_utc: int = Form(0),
    daily_minute_utc: int = Form(30),
    rate_per_sec: float = Form(1.5),
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
        "SOLIS_FEED_IN_TARIFF": float(feed_in_tariff),
        "SOLIS_IMPORT_TARIFF": float(import_tariff),
        "SOLIS_CURRENCY": currency.strip() or "EUR",
        # The unchecked-checkbox case sends no value at all, hence the
        # `None` default — anything else is a True signal.
        "RUN_SCHEDULER": run_scheduler is not None,
        "SCHEDULER_SAMPLE_MINUTES": max(1, int(sample_minutes)),
        "SCHEDULER_DAILY_HOUR_UTC": max(0, min(23, int(daily_hour_utc))),
        "SCHEDULER_DAILY_MINUTE_UTC": max(0, min(59, int(daily_minute_utc))),
        "SCHEDULER_RATE_PER_SEC": max(0.1, min(2.0, float(rate_per_sec))),
    }
    # Only overwrite the secret if the user actually entered a new one.
    if solis_keysecret:
        updates["SOLIS_KEYSECRET"] = solis_keysecret

    write_toml(updates)
    get_settings.cache_clear()
    await _invalidate_clients(request.app)
    await _ensure_scheduler_running(request.app)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "config_path": str(user_config_toml_path()),
            "defaults": _setup_defaults(),
            "error": None,
            "saved": True,
            "purged_counts": None,
        },
    )


# Purge now lives on the Data tab — see `/data/purge` below.


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


# --- Data sync / management tab --------------------------------------------


@app.get("/data", response_class=HTMLResponse, response_model=None)
async def data_page(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
) -> HTMLResponse:
    """Hub for syncing with SolisCloud and managing local data.

    Hosts Poll-now, Fetch-historical, and Purge so the History page can
    stay focused on visualising data instead of capturing it."""
    last_sample = await db["station_samples"].find_one(sort=[("ts", -1)])
    last_sample_ts: Any = last_sample.get("ts") if last_sample else None
    if isinstance(last_sample_ts, datetime) and last_sample_ts.tzinfo is None:
        last_sample_ts = last_sample_ts.replace(tzinfo=timezone.utc)
    station_count = await db["stations"].count_documents({})
    sample_count = await db["station_samples"].estimated_document_count()
    daily_count = await db["station_daily"].estimated_document_count()
    today = datetime.now(timezone.utc).date().isoformat()
    return templates.TemplateResponse(
        request,
        "data.html",
        {
            "user": user,
            "last_sample_ts": last_sample_ts,
            "station_count": station_count,
            "sample_count": sample_count,
            "daily_count": daily_count,
            "today": today,
            "backfill_default_start": f"{int(today[:4]) - 1:04d}-01-01",
            "purged_counts": None,
        },
    )


@app.post("/data/purge", response_class=HTMLResponse, response_model=None)
async def data_purge(
    request: Request,
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    """Drop every row of SolisCloud-sourced data, keep admin accounts and
    config. The drop set is `SOLISCLOUD_COLLECTIONS` — by definition the
    collections every row of which is re-downloadable, so a Poll-now or
    Fetch from the /data tab puts the dashboard back to where it was.
    Local state (`users`, the `solisdash.toml` config) is never touched."""
    counts: dict[str, int] = {}
    for collection in SOLISCLOUD_COLLECTIONS:
        result = await db[collection].delete_many({})
        counts[collection] = result.deleted_count
    logging.getLogger(__name__).info("purged downloaded data: %s", counts)
    today = datetime.now(timezone.utc).date().isoformat()
    return templates.TemplateResponse(
        request,
        "data.html",
        {
            "user": user,
            "last_sample_ts": None,
            "station_count": 0,
            "sample_count": 0,
            "daily_count": 0,
            "today": today,
            "backfill_default_start": f"{int(today[:4]) - 1:04d}-01-01",
            "purged_counts": counts,
        },
    )


# --- History page -----------------------------------------------------------


@app.get("/history", response_class=HTMLResponse, response_model=None)
async def history_page(
    request: Request,
    user: dict[str, Any] = Depends(require_user),
    history: HistoryService = Depends(get_history_service),
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
) -> HTMLResponse:
    stations = await history.list_stations()
    selected = stations[0]["id"] if stations else None
    # Default tab + range chosen by which collection actually has data, so
    # the user lands on a populated chart rather than a "No data" panel:
    #   - station_daily present  → Energy / Month (the bars they just
    #                              downloaded via Fetch or Poll)
    #   - only station_samples   → Power / Day (the live curve)
    #   - neither                → Power / Day (the empty-state poll
    #                              button takes over anyway)
    has_daily = bool(
        stations and await db["station_daily"].count_documents({}, limit=1)
    )
    default_metric = "energy" if has_daily else "power"
    default_view = "month" if has_daily else "day"
    today_date = datetime.now(timezone.utc).date()
    # Default range: last 30 days. Wide enough to show this month's daily
    # bars when Energy is the default metric; auto_range clamps to the
    # last 7 days when the active metric is sample-only (Power / Battery
    # / Alarms), so it doesn't pull tens of thousands of sample points.
    default_range_start = (today_date - timedelta(days=29)).isoformat()
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
            # Default backfill window: from Jan 1 of last year. Wide enough
            # to fill in a Year and All-time view comfortably, narrow enough
            # not to spam SolisCloud on the first click.
            "backfill_default_start": f"{int(today[:4]) - 1:04d}-01-01",
            "default_metric": default_metric,
            "default_view": default_view,
            "default_range_start": default_range_start,
        },
    )


async def _run_poll_now(
    task_id: str,
    poller: Poller,
    station_ids: list[str],
    months: list[str],
    today: date,
) -> None:
    """First-run quick fill: current state per station, all of this year's
    daily rollups, plus today's intra-day 5-minute curve.

    Older days aren't intraday-backfilled here — that's the dedicated
    Fetch panel's job. Drives the progress bar via the shared
    `_BACKFILL_TASKS` dict."""
    state = _BACKFILL_TASKS[task_id]
    try:
        for sid in station_ids:
            sample = await poller.poll_current(sid)
            if sample is not None:
                state["rows_written"] += 1
            state["done"] += 1
        for sid in station_ids:
            for month in months:
                written = await poller.poll_daily_for_month(sid, month)
                state["rows_written"] += written
                state["done"] += 1
        for sid in station_ids:
            written = await poller.poll_intraday_for_date(sid, today)
            state["rows_written"] += written
            state["done"] += 1
        state["status"] = "done"
    except SolisAPIError as exc:
        state["status"] = "error"
        state["error"] = f"SolisCloud rejected the call: [{exc.code}] {exc.msg}"
    except (httpx.HTTPError, Exception) as exc:
        state["status"] = "error"
        state["error"] = f"{type(exc).__name__}: {exc}"


@app.post("/history/poll-now", response_class=HTMLResponse, response_model=None)
async def history_poll_now(
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
    solis: SolisClient = Depends(get_solis_client),
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    """Kick off a background "first-run" fetch: pulls `stationDetail` per
    station (live tile data + `stations` upsert) plus this year's daily
    rollups via `stationMonth`. Returns a polling fragment so the user
    sees the progress bar tick rather than a 30-second blank wait."""
    poller = Poller(solis=solis, db=db)
    today = datetime.now(timezone.utc).date()
    year_start = today.replace(month=1, day=1)

    try:
        station_ids = await poller.list_station_ids()
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
    if not station_ids:
        return HTMLResponse(
            '<p role="alert" class="error">SolisCloud returned no stations for '
            'this account. Check the API key in <a href="/settings">Settings</a>.</p>'
        )

    months = list(iter_months(year_start, today))
    # Tick budget: 1 stationDetail + N stationMonth + 1 stationDay(today)
    # per station, all running through the rate-limited poller.
    total = len(station_ids) * (1 + len(months) + 1)
    task_id = secrets.token_urlsafe(8)
    _BACKFILL_TASKS[task_id] = {
        "done": 0,
        "total": total,
        "rows_written": 0,
        "status": "running",
        "error": None,
        "started_at": datetime.now(timezone.utc),
    }
    _BACKFILL_TASKS[task_id]["task"] = asyncio.create_task(
        _run_poll_now(task_id, poller, station_ids, months, today)
    )
    return HTMLResponse(_backfill_progress_fragment(task_id))


# --- backfill background tasks --------------------------------------------
#
# `POST /history/backfill` kicks off an asyncio task and returns immediately
# with a progress fragment that polls `GET /history/backfill/status/{id}`
# every 700ms. The status endpoint renders an updating progress bar until
# the task finishes, then emits HX-Refresh so the charts pick up the new
# rows. Single-process in-memory tracker — fine for a self-hosted dashboard;
# bigger deployments would put this in Redis or similar.

_BACKFILL_TASKS: dict[str, dict[str, Any]] = {}


def _backfill_progress_fragment(task_id: str) -> str:
    """Render the polling element that hits /history/backfill/status."""
    state = _BACKFILL_TASKS.get(task_id)
    if state is None:
        return (
            '<p class="test-result error">✗ Backfill task expired. '
            "Try again — the server may have been restarted.</p>"
        )
    done = state["done"]
    total = state["total"]
    rows = state["rows_written"]
    label = (
        f"Downloading <strong>{done} / {total}</strong> month(s) "
        f"(<strong>{rows}</strong> daily row(s) written so far)…"
    )
    if total == 0:
        bar = '<progress style="width: 100%;"></progress>'
    else:
        bar = (
            f'<progress value="{done}" max="{total}" style="width: 100%;"></progress>'
        )
    # Self-polling: the returned fragment re-fetches itself every 700ms via
    # `hx-trigger="every 700ms"`. We can't use `load delay:700ms` because the
    # `load` HTMX event only fires reliably on the first insertion — when an
    # outerHTML swap replaces the same-id element with a fresh copy, `load`
    # often doesn't re-fire, so the polling chain dies after one tick.
    # `every` sets up a real interval that survives swaps.
    # The parent <form> sets `hx-disinherit="hx-disabled-elt …"` to block
    # the form's `hx-disabled-elt="find button"` from leaking down here —
    # without that, HTMX inherits the selector onto every polling tick,
    # finds no <button> in this fragment, fires htmx:targetError, and
    # aborts the swap. The progress bar then sits frozen at 0/N forever.
    return (
        f'<div id="backfill-progress" '
        f'hx-get="/history/backfill/status/{task_id}" '
        f'hx-trigger="every 700ms" '
        f'hx-swap="outerHTML" '
        f'aria-live="polite">'
        f"{bar}"
        f'<p class="muted">{label}</p>'
        f"</div>"
    )


async def _run_backfill(
    task_id: str,
    poller: Poller,
    station_ids: list[str],
    months: list[str],
    dates: list[date],
) -> None:
    """Walk every (station, month) pair upserting daily rollups, then walk
    every (station, day) pair upserting 5-minute intraday samples.

    Two passes so the chart populates progressively — Energy/Charge/
    Discharge tabs fill in first (cheap, one call per month), Power/
    Battery curves catch up after (one call per day)."""
    state = _BACKFILL_TASKS[task_id]
    try:
        for sid in station_ids:
            for month in months:
                written = await poller.poll_daily_for_month(sid, month)
                state["rows_written"] += written
                state["done"] += 1
        for sid in station_ids:
            for when in dates:
                written = await poller.poll_intraday_for_date(sid, when)
                state["rows_written"] += written
                state["done"] += 1
        state["status"] = "done"
    except SolisAPIError as exc:
        state["status"] = "error"
        state["error"] = f"SolisCloud rejected the call: [{exc.code}] {exc.msg}"
    except (httpx.HTTPError, Exception) as exc:
        state["status"] = "error"
        state["error"] = f"{type(exc).__name__}: {exc}"


@app.post("/history/backfill", response_class=HTMLResponse, response_model=None)
async def history_backfill(
    start: str = Form(..., description="YYYY-MM-DD inclusive"),
    end: str = Form(..., description="YYYY-MM-DD inclusive"),
    db: AsyncDatabase[dict[str, Any]] = Depends(get_db),
    solis: SolisClient = Depends(get_solis_client),
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    """Kick off a background backfill task and return a polling fragment.

    The actual `stationMonth` calls happen in `_run_backfill`. The client
    polls `/history/backfill/status/{task_id}` and sees the progress bar
    advance until completion."""
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as exc:
        return HTMLResponse(
            f'<p class="test-result error">✗ Bad date: {exc}</p>', status_code=200
        )
    if end_d < start_d:
        return HTMLResponse(
            '<p class="test-result error">✗ End date must not be before start date.</p>',
            status_code=200,
        )

    poller = Poller(solis=solis, db=db)
    try:
        station_ids = await poller.list_station_ids()
    except SolisAPIError as exc:
        return HTMLResponse(
            f'<p class="test-result error">✗ SolisCloud rejected the call: '
            f"[{exc.code}] {exc.msg}.</p>",
            status_code=200,
        )
    except httpx.HTTPError as exc:
        return HTMLResponse(
            f'<p class="test-result error">✗ Could not reach SolisCloud: '
            f"{type(exc).__name__}: {exc}</p>",
            status_code=200,
        )
    if not station_ids:
        return HTMLResponse(
            '<p class="test-result error">✗ SolisCloud returned no stations '
            "for this account.</p>",
            status_code=200,
        )

    months = list(iter_months(start_d, end_d))
    dates = list(iter_dates(start_d, end_d))
    # One tick per (station, month) for daily rollups + one tick per
    # (station, day) for the intraday 5-min curve.
    total = len(station_ids) * (len(months) + len(dates))
    task_id = secrets.token_urlsafe(8)
    _BACKFILL_TASKS[task_id] = {
        "done": 0,
        "total": total,
        "rows_written": 0,
        "status": "running",
        "error": None,
        "started_at": datetime.now(timezone.utc),
    }
    # Store a strong reference to the task so the GC can't reap it mid-flight
    # (asyncio.create_task's reference is weak — its docstring explicitly
    # warns about this).
    _BACKFILL_TASKS[task_id]["task"] = asyncio.create_task(
        _run_backfill(task_id, poller, station_ids, months, dates)
    )
    return HTMLResponse(_backfill_progress_fragment(task_id))


@app.get(
    "/history/backfill/status/{task_id}",
    response_class=HTMLResponse,
    response_model=None,
)
async def history_backfill_status(
    task_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> HTMLResponse:
    """HTMX polling target. Renders the current progress bar; on success
    sets HX-Refresh so the page reloads and the charts pick up new rows."""
    state = _BACKFILL_TASKS.get(task_id)
    if state is None:
        return HTMLResponse(
            '<p class="test-result error">✗ Unknown backfill task. '
            "It may have been cleared by a server restart.</p>"
        )
    if state["status"] == "error":
        return HTMLResponse(
            f'<p class="test-result error">✗ {state["error"]}</p>'
        )
    if state["status"] == "done":
        rows = state["rows_written"]
        total = state["total"]
        return HTMLResponse(
            f'<p class="test-result success">✓ Backfilled {rows} daily row(s) '
            f"across {total} month-call(s). Reloading…</p>",
            headers={"HX-Refresh": "true"},
        )
    return HTMLResponse(_backfill_progress_fragment(task_id))


@app.get("/history/range.json")
async def history_range_json(
    station_id: str | None = Query(None),
    start: str = Query(..., description="YYYY-MM-DD inclusive"),
    end: str = Query(..., description="YYYY-MM-DD inclusive"),
    metric: str = Query(METRIC_ENERGY),
    resolution: str = Query("auto", description="auto | samples | daily | monthly | yearly"),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    """Auto-resolution range query.

    The History page's only chart endpoint. Picks samples / daily /
    monthly / yearly resolution based on (metric, span) and reports back
    which one it used so the chart can label its x-axis sensibly."""
    if metric not in METRIC_SUPPORTS:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric!r}")
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return JSONResponse(
            {
                "station_id": None,
                "label": "",
                "unit": "",
                "points": [],
                "resolution": "none",
                "effective_start": start_d.isoformat(),
                "effective_end": end_d.isoformat(),
            }
        )
    settings = get_settings()
    try:
        series, resolution_label, eff_start, eff_end = await history.auto_range(
            sid,
            start_d,
            end_d,
            metric=metric,
            requested_resolution=resolution,
            feed_in_tariff=settings.SOLIS_FEED_IN_TARIFF,
            import_tariff=settings.SOLIS_IMPORT_TARIFF,
            currency=settings.SOLIS_CURRENCY,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    resolution = resolution_label  # rename for the JSON field below
    return JSONResponse(
        {
            "station_id": sid,
            "resolution": resolution,
            "effective_start": eff_start.isoformat(),
            "effective_end": eff_end.isoformat(),
            **series.to_json(),
        }
    )


@app.get("/history/range.csv")
async def history_range_csv(
    station_id: str | None = Query(None),
    start: str = Query(..., description="YYYY-MM-DD inclusive"),
    end: str = Query(..., description="YYYY-MM-DD inclusive"),
    metric: str = Query(METRIC_ENERGY),
    resolution: str = Query("auto"),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    if metric not in METRIC_SUPPORTS:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric!r}")
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response(f"x,{metric}\n", f"history-{metric}.csv")
    settings = get_settings()
    try:
        series, _resolution, _start, _end = await history.auto_range(
            sid,
            start_d,
            end_d,
            metric=metric,
            requested_resolution=resolution,
            feed_in_tariff=settings.SOLIS_FEED_IN_TARIFF,
            import_tariff=settings.SOLIS_IMPORT_TARIFF,
            currency=settings.SOLIS_CURRENCY,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _csv_response(
        _series_to_csv(series, x_header="x", y_header=metric),
        f"history-{metric}-{sid}-{start}-{end}.csv",
    )


_DAY_LABELS = {
    "power": ("Power", "kW"),
    "energy": ("Day energy", "kWh"),
    "battery": ("Battery SOC", "%"),
    "alarms": ("Open alarms", ""),
}
_DAILY_LABELS = {
    "energy": {
        "month": ("Daily energy", "kWh"),
        "year": ("Monthly energy", "kWh"),
        "all": ("Annual energy", "kWh"),
    },
    "money": {
        "month": ("Daily revenue", ""),
        "year": ("Monthly revenue", ""),
        "all": ("Annual revenue", ""),
    },
}


def _validate_metric(metric: str, view: str) -> None:
    if metric not in METRIC_SUPPORTS:
        raise HTTPException(status_code=400, detail=f"unknown metric: {metric!r}")
    if not metric_supports(metric, view):
        raise HTTPException(
            status_code=400,
            detail=f"metric {metric!r} is not available for the {view!r} view",
        )


@app.get("/history/day.json")
async def history_day_json(
    station_id: str | None = Query(None),
    when: str = Query(..., description="YYYY-MM-DD"),
    metric: str = Query(METRIC_POWER),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    _validate_metric(metric, VIEW_DAY)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        label, unit = _DAY_LABELS.get(metric, ("Series", ""))
        return JSONResponse(_empty_series_response(label, unit, station_id=None))
    try:
        when_date = date.fromisoformat(when)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.day_series(sid, when_date, metric=metric)
    return JSONResponse({"station_id": sid, **series.to_json()})


@app.get("/history/month.json")
async def history_month_json(
    station_id: str | None = Query(None),
    month: str = Query(..., description="YYYY-MM"),
    metric: str = Query(METRIC_ENERGY),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    _validate_metric(metric, VIEW_MONTH)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        label, unit = _DAILY_LABELS[metric]["month"]
        return JSONResponse(_empty_series_response(label, unit, station_id=None))
    try:
        parse_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.month_daily(sid, month, metric=metric)
    return JSONResponse({"station_id": sid, **series.to_json()})


@app.get("/history/year.json")
async def history_year_json(
    station_id: str | None = Query(None),
    year: int = Query(...),
    metric: str = Query(METRIC_ENERGY),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    _validate_metric(metric, VIEW_YEAR)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        label, unit = _DAILY_LABELS[metric]["year"]
        return JSONResponse(_empty_series_response(label, unit, station_id=None))
    series = await history.year_monthly(sid, year, metric=metric)
    return JSONResponse({"station_id": sid, **series.to_json()})


@app.get("/history/all.json")
async def history_all_json(
    station_id: str | None = Query(None),
    metric: str = Query(METRIC_ENERGY),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> JSONResponse:
    _validate_metric(metric, VIEW_ALL)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        label, unit = _DAILY_LABELS[metric]["all"]
        return JSONResponse(_empty_series_response(label, unit, station_id=None))
    series = await history.all_time(sid, metric=metric)
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
    metric: str = Query(METRIC_POWER),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    _validate_metric(metric, VIEW_DAY)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response(f"timestamp_ms,{metric}\n", f"history-day-{metric}.csv")
    try:
        when_date = date.fromisoformat(when)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.day_series(sid, when_date, metric=metric)
    return _csv_response(
        _series_to_csv(series, x_header="timestamp_ms", y_header=metric),
        f"history-day-{metric}-{sid}-{when}.csv",
    )


@app.get("/history/month.csv")
async def history_month_csv(
    station_id: str | None = Query(None),
    month: str = Query(..., description="YYYY-MM"),
    metric: str = Query(METRIC_ENERGY),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    _validate_metric(metric, VIEW_MONTH)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response(f"date,{metric}\n", f"history-month-{metric}.csv")
    try:
        parse_month(month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    series = await history.month_daily(sid, month, metric=metric)
    return _csv_response(
        _series_to_csv(series, x_header="date", y_header=metric),
        f"history-month-{metric}-{sid}-{month}.csv",
    )


@app.get("/history/year.csv")
async def history_year_csv(
    station_id: str | None = Query(None),
    year: int = Query(...),
    metric: str = Query(METRIC_ENERGY),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    _validate_metric(metric, VIEW_YEAR)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response(f"month,{metric}\n", f"history-year-{metric}.csv")
    series = await history.year_monthly(sid, year, metric=metric)
    return _csv_response(
        _series_to_csv(series, x_header="month", y_header=metric),
        f"history-year-{metric}-{sid}-{year}.csv",
    )


@app.get("/history/all.csv")
async def history_all_csv(
    station_id: str | None = Query(None),
    metric: str = Query(METRIC_ENERGY),
    history: HistoryService = Depends(get_history_service),
    user: dict[str, Any] = Depends(require_user),
) -> Response:
    _validate_metric(metric, VIEW_ALL)
    sid = await _resolve_station_id(history, station_id)
    if sid is None:
        return _csv_response(f"year,{metric}\n", f"history-all-{metric}.csv")
    series = await history.all_time(sid, metric=metric)
    return _csv_response(
        _series_to_csv(series, x_header="year", y_header=metric),
        f"history-all-{metric}-{sid}.csv",
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
