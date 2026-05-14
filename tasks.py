from __future__ import annotations

import asyncio
import getpass
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from invoke.context import Context
from invoke.tasks import task

ROOT = Path(__file__).parent
VAR = ROOT / "var"
PIDFILE = VAR / "uvicorn.pid"
INFOFILE = VAR / "uvicorn.info.json"
LOGFILE = VAR / "uvicorn.log"
APP = "solisdash.app:app"

DEFAULT_HOST = os.environ.get("HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PORT", "8000"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid() -> int | None:
    if not PIDFILE.exists():
        return None
    try:
        return int(PIDFILE.read_text().strip())
    except ValueError:
        return None


def _read_runtime_info() -> dict[str, Any]:
    """Host + port the running server was started with, or defaults."""
    if INFOFILE.exists():
        try:
            data = json.loads(INFOFILE.read_text())
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"host": DEFAULT_HOST, "port": DEFAULT_PORT}


def _write_runtime_info(*, host: str, port: int) -> None:
    INFOFILE.write_text(json.dumps({"host": host, "port": port}))


@task
def start(
    c: Context,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    reload: bool = False,
) -> None:
    """Start the FastAPI app under uvicorn (detached)."""
    VAR.mkdir(exist_ok=True)
    pid = _read_pid()
    if pid is not None and _alive(pid):
        print(f"already running (pid {pid})")
        return
    if pid is not None:
        PIDFILE.unlink(missing_ok=True)

    reload_flag = " --reload" if reload else ""
    cmd = (
        f"nohup uv run python -m uvicorn {APP} --host {host} --port {port}{reload_flag} "
        f">> {LOGFILE} 2>&1 & echo $! > {PIDFILE}"
    )
    c.run(cmd, pty=False)
    _write_runtime_info(host=host, port=port)
    print(f"started on http://{host}:{port} (logs: {LOGFILE})")
    status(c)


@task
def stop(c: Context) -> None:
    """Stop the FastAPI app."""
    pid = _read_pid()
    if pid is None:
        print("not running")
        INFOFILE.unlink(missing_ok=True)
        return
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}")
    else:
        print(f"stale pidfile (pid {pid} dead)")
    PIDFILE.unlink(missing_ok=True)
    INFOFILE.unlink(missing_ok=True)


@task
def restart(
    c: Context,
    host: str | None = None,
    port: int | None = None,
    reload: bool = False,
) -> None:
    """Restart the FastAPI app. Preserves the previous host/port unless overridden."""
    previous = _read_runtime_info()
    stop(c)
    start(
        c,
        host=host if host is not None else str(previous.get("host", DEFAULT_HOST)),
        port=port if port is not None else int(previous.get("port", DEFAULT_PORT)),
        reload=reload,
    )


@task
def status(c: Context) -> None:
    """Show server status and probe /health on the port the server was started with."""
    pid = _read_pid()
    if pid is None:
        print("not running")
        return
    if not _alive(pid):
        print(f"stale pidfile (pid {pid} dead)")
        return
    info = _read_runtime_info()
    host = str(info.get("host", DEFAULT_HOST))
    port = int(info.get("port", DEFAULT_PORT))
    print(f"running (pid {pid}) on http://{host}:{port}")
    c.run(
        f"curl -fsS http://{host}:{port}/health || echo 'health probe failed'",
        warn=True,
    )


@task
def test(c: Context) -> None:
    """Run the full test suite in parallel."""
    try:
        c.run("uv run python -m pytest -n auto", pty=True)
    except KeyboardInterrupt:
        sys.exit(130)


@task
def lint(c: Context) -> None:
    """Run ruff and mypy."""
    c.run("uv run python -m ruff check .", pty=True)
    c.run("uv run python -m mypy", pty=True, warn=True)


@task(help={"username": "Username", "role": "admin or user (default: user)"})
def add_user(c: Context, username: str, role: str = "user") -> None:
    """Insert a user into the `users` collection. Prompts for password."""
    from pymongo import AsyncMongoClient
    from pymongo.errors import DuplicateKeyError

    from solisdash.auth import ROLES, create_user
    from solisdash.config import get_settings
    from solisdash.db import ensure_indexes

    if role not in ROLES:
        print(f"role must be one of {ROLES}, got {role!r}", file=sys.stderr)
        sys.exit(2)

    settings = get_settings()
    if not settings.SOLIS_MONGODB_URI:
        print("SOLIS_MONGODB_URI is not set", file=sys.stderr)
        sys.exit(2)

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm:  ")
    if password != confirm:
        print("passwords do not match", file=sys.stderr)
        sys.exit(2)
    if not password:
        print("password must not be empty", file=sys.stderr)
        sys.exit(2)

    async def _go() -> None:
        client: AsyncMongoClient[dict] = AsyncMongoClient(settings.SOLIS_MONGODB_URI)  # type: ignore[type-arg]
        try:
            db = client[settings.SOLIS_MONGODB_DB]
            await ensure_indexes(db)
            try:
                await create_user(db, username=username, password=password, role=role)
            except DuplicateKeyError:
                print(f"user {username!r} already exists", file=sys.stderr)
                sys.exit(1)
            print(f"created {role} {username!r}")
        finally:
            await client.close()

    asyncio.run(_go())


def _build_poller_pieces() -> tuple[Any, Any, Any]:
    """Construct (mongo_client, db, poller) for one-shot CLI tasks."""
    from pymongo import AsyncMongoClient

    from solisdash.client import SolisClient
    from solisdash.config import get_settings
    from solisdash.poller import Poller

    settings = get_settings()
    if not settings.SOLIS_MONGODB_URI:
        print("SOLIS_MONGODB_URI is not set", file=sys.stderr)
        sys.exit(2)
    if not settings.SOLIS_KEY_ID or not settings.SOLIS_KEYSECRET:
        print("SOLIS_KEY_ID / SOLIS_KEYSECRET not set", file=sys.stderr)
        sys.exit(2)

    mongo: AsyncMongoClient[dict] = AsyncMongoClient(  # type: ignore[type-arg]
        settings.SOLIS_MONGODB_URI
    )
    db = mongo[settings.SOLIS_MONGODB_DB]
    solis = SolisClient(
        base_url=settings.SOLIS_API_URL,
        key_id=settings.SOLIS_KEY_ID,
        key_secret=settings.SOLIS_KEYSECRET,
    )
    poller = Poller(solis=solis, db=db)
    return mongo, solis, poller


@task
def poll_once(c: Context) -> None:
    """Pull stationDetail for every station and upsert into station_samples."""
    from solisdash.db import ensure_indexes

    mongo, solis, poller = _build_poller_pieces()

    async def _go() -> None:
        try:
            await ensure_indexes(poller._db)
            n = await poller.poll_current_all()
            print(f"wrote {n} snapshot(s)")
        finally:
            await solis.aclose()
            await mongo.close()

    asyncio.run(_go())


@task(help={"start": "YYYY-MM-DD inclusive", "end": "YYYY-MM-DD inclusive"})
def backfill(c: Context, start: str, end: str) -> None:
    """Backfill daily rollups for every station between two dates (inclusive)."""
    from datetime import date

    from solisdash.db import ensure_indexes

    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as exc:
        print(f"bad date: {exc}", file=sys.stderr)
        sys.exit(2)

    mongo, solis, poller = _build_poller_pieces()

    async def _go() -> None:
        try:
            await ensure_indexes(poller._db)
            counts = await poller.backfill_daily(start=start_d, end=end_d)
            for sid, n in counts.items():
                print(f"  {sid}: {n} day(s)")
            print(f"backfilled {sum(counts.values())} day(s) across {len(counts)} station(s)")
        finally:
            await solis.aclose()
            await mongo.close()

    asyncio.run(_go())
