from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from invoke.context import Context
from invoke.tasks import task

ROOT = Path(__file__).parent
VAR = ROOT / "var"
PIDFILE = VAR / "uvicorn.pid"
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
    print(f"started on http://{host}:{port} (logs: {LOGFILE})")
    status(c)


@task
def stop(c: Context) -> None:
    """Stop the FastAPI app."""
    pid = _read_pid()
    if pid is None:
        print("not running")
        return
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}")
    else:
        print(f"stale pidfile (pid {pid} dead)")
    PIDFILE.unlink(missing_ok=True)


@task
def restart(c: Context) -> None:
    """Restart the FastAPI app."""
    stop(c)
    start(c)


@task
def status(c: Context) -> None:
    """Show server status and probe /health."""
    pid = _read_pid()
    if pid is None:
        print("not running")
        return
    if not _alive(pid):
        print(f"stale pidfile (pid {pid} dead)")
        return
    print(f"running (pid {pid})")
    c.run(
        f"curl -fsS http://{DEFAULT_HOST}:{DEFAULT_PORT}/health || echo 'health probe failed'",
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
