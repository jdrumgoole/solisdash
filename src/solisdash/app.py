"""Solisdash FastAPI app: session auth, Jinja shell, lazy-connected Mongo."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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
from solisdash.config import get_settings
from solisdash.db import ensure_indexes

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
    """Close the Mongo client on shutdown if `get_db` opened one."""
    app.state.mongo_client = None
    try:
        yield
    finally:
        client: AsyncMongoClient[dict[str, Any]] | None = app.state.mongo_client
        if client is not None:
            await client.close()


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


@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request, user: dict[str, Any] = Depends(require_user)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "home.html", {"user": user, "version": __version__}
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
