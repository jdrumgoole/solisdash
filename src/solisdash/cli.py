"""`solisdash` CLI: run the dashboard, or add a user.

Subcommands:
- (no subcommand) / `run` — open the dashboard inside a pywebview window.
- `add-user`             — seed an additional account (the first one is
                            created from the browser-side setup wizard).

Pure-Python entry point installed by the wheel. No `uv` / `invoke` /
external tooling at runtime — just `pip install solisdash && solisdash`.

All user-facing configuration happens inside the webview at `/setup`
(first run) or `/settings` (later). The CLI does not prompt for
anything except passwords on `add-user`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

import httpx
import uvicorn

from solisdash import __version__

log = logging.getLogger("solisdash.cli")

REPO_URL = "https://github.com/jdrumgoole/solisdash"


def find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for a port nobody else is using right now.

    There's a race between this and uvicorn binding it, but the window's
    tiny on a single-user dev machine.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def wait_for_health(
    base_url: str,
    *,
    timeout_s: float = 15.0,
    interval_s: float = 0.1,
) -> None:
    """Poll `/health` until uvicorn is actually serving."""
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.RequestError as exc:
            last_exc = exc
        time.sleep(interval_s)
    raise RuntimeError(
        f"App did not become healthy within {timeout_s:.1f}s: {last_exc!r}"
    )


class UvicornServerThread(threading.Thread):
    """Background uvicorn so pywebview can own the main thread (macOS needs that)."""

    def __init__(self, *, host: str, port: int, log_level: str = "info") -> None:
        super().__init__(name="uvicorn", daemon=True)
        config = uvicorn.Config(
            "solisdash.app:app",
            host=host,
            port=port,
            log_level=log_level,
        )
        self.server = uvicorn.Server(config)
        self.host = host
        self.port = port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def run(self) -> None:
        try:
            self.server.run()
        except Exception:
            log.exception("uvicorn crashed")

    def stop(self) -> None:
        self.server.should_exit = True


def _icon_path() -> str | None:
    """Locate the bundled icon. Returns None if it can't be found."""
    try:
        path = files("solisdash").joinpath("static/icon.png")
        return str(path) if Path(str(path)).exists() else None
    except (FileNotFoundError, ModuleNotFoundError):
        return None


def _build_menu(base_url: str) -> list[object]:
    """Native menu wired to pywebview's window."""
    import webview
    from webview.menu import Menu, MenuAction, MenuSeparator

    def _load(path: str) -> None:
        if webview.windows:
            webview.windows[0].load_url(f"{base_url}{path}")

    def _reload() -> None:
        if webview.windows:
            current = webview.windows[0].get_current_url() or base_url
            webview.windows[0].load_url(current)

    def _quit() -> None:
        for w in list(webview.windows):
            w.destroy()

    def _github() -> None:
        webbrowser.open(REPO_URL)

    return [
        Menu(
            "Solisdash",
            [
                MenuAction("Open in browser", lambda: webbrowser.open(base_url)),
                MenuSeparator(),
                MenuAction("Quit Solisdash", _quit),
            ],
        ),
        Menu(
            "View",
            [
                MenuAction("Dashboard", lambda: _load("/")),
                MenuAction("History", lambda: _load("/history")),
                MenuAction("Alarms", lambda: _load("/alarms")),
                MenuAction("Settings", lambda: _load("/settings")),
                MenuSeparator(),
                MenuAction("Reload", _reload),
            ],
        ),
        Menu(
            "Help",
            [
                MenuAction("Solisdash on GitHub", _github),
                MenuAction(f"Version: {__version__}", lambda: None),
            ],
        ),
    ]


def launch_webview(base_url: str, *, debug: bool = False) -> None:
    """Open the native window. Blocks until the window is closed."""
    import webview

    icon = _icon_path()
    if icon is None:
        log.warning("Bundled icon not found; window will use the platform default.")

    webview.create_window(
        title=f"Solisdash v{__version__}",
        url=base_url,
        width=1200,
        height=800,
        min_size=(800, 600),
    )
    start_kwargs: dict[str, object] = {"menu": _build_menu(base_url), "debug": debug}
    if icon is not None:
        start_kwargs["icon"] = icon
    webview.start(**start_kwargs)  # type: ignore[arg-type]


def _ensure_session_secret() -> None:
    """Silently generate a `SESSION_SECRET` on first boot if there isn't one.

    Without this, `SessionMiddleware` falls back to a static placeholder and
    cookies don't survive a restart. The setup wizard would also do this on
    first save, but generating eagerly here means even an admin who only
    configures via env vars / hand-edited toml gets a secure default.
    """
    from solisdash.config import get_settings
    from solisdash.configfile import user_config_toml_path, write_toml

    settings = get_settings()
    if settings.SESSION_SECRET:
        return
    secret = secrets.token_urlsafe(32)
    write_toml({"SESSION_SECRET": secret})
    get_settings.cache_clear()
    log.info("Generated SESSION_SECRET and saved to %s", user_config_toml_path())


# --- subcommand: run -------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _ensure_session_secret()
    port = args.port or find_free_port(args.host)
    server = UvicornServerThread(
        host=args.host,
        port=port,
        log_level="debug" if args.debug else "info",
    )
    server.start()
    try:
        wait_for_health(server.base_url, timeout_s=args.timeout)
    except RuntimeError as exc:
        log.error("Startup failed: %s", exc)
        server.stop()
        return 1

    log.info("Serving at %s", server.base_url)
    try:
        if args.no_window:
            print(f"Serving at {server.base_url}. Ctrl-C to stop.")
            try:
                server.join()
            except KeyboardInterrupt:
                pass
        else:
            launch_webview(server.base_url, debug=args.debug)
    finally:
        server.stop()
    return 0


# --- subcommand: add-user --------------------------------------------------


def _prompt_password(getpass_fn: Callable[[str], str] = getpass.getpass) -> str | None:
    """Prompt for a password twice; return the value or `None` on mismatch/empty.

    Indirection on `getpass.getpass` is so tests can inject a stub.
    """
    pw1 = getpass_fn("Password: ")
    pw2 = getpass_fn("Confirm:  ")
    if pw1 != pw2:
        print("passwords do not match", file=sys.stderr)
        return None
    if not pw1:
        print("password must not be empty", file=sys.stderr)
        return None
    return pw1


def cmd_add_user(args: argparse.Namespace) -> int:
    """Create a user in the configured Mongo. Prompts for the password."""
    from pymongo import AsyncMongoClient
    from pymongo.errors import DuplicateKeyError

    from solisdash.auth import ROLES, create_user
    from solisdash.config import get_settings
    from solisdash.db import ensure_indexes

    if args.role not in ROLES:
        print(f"role must be one of {ROLES}, got {args.role!r}", file=sys.stderr)
        return 2

    settings = get_settings()
    if not settings.SOLIS_MONGODB_URI:
        print(
            "SOLIS_MONGODB_URI is not set. Configure it via the in-browser "
            "Settings page, or set the env var directly.",
            file=sys.stderr,
        )
        return 2

    password = _prompt_password()
    if password is None:
        return 2

    async def _go() -> int:
        client: AsyncMongoClient[dict[str, object]] = AsyncMongoClient(
            settings.SOLIS_MONGODB_URI
        )
        try:
            db = client[settings.SOLIS_MONGODB_DB]
            await ensure_indexes(db)
            try:
                await create_user(
                    db, username=args.username, password=password, role=args.role
                )
            except DuplicateKeyError:
                print(f"user {args.username!r} already exists", file=sys.stderr)
                return 1
            print(f"created {args.role} {args.username!r}")
            return 0
        finally:
            await client.close()

    return asyncio.run(_go())


# --- argparse plumbing -----------------------------------------------------


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Address uvicorn binds to. Default: 127.0.0.1 (loopback only).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to bind. 0 (default) picks a free one.",
    )
    p.add_argument(
        "--no-window",
        action="store_true",
        help="Run uvicorn without opening pywebview (useful for ssh / headless smoke).",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Open pywebview's dev tools and use uvicorn debug logging.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for the app to become healthy before giving up.",
    )


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="solisdash",
        description=(
            "Solisdash CLI. With no subcommand, runs the dashboard in a "
            "native pywebview window. All configuration happens in-browser."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"solisdash {__version__}",
    )
    _add_run_args(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    run_p = subparsers.add_parser(
        "run",
        help="Run the dashboard in a pywebview window (default).",
        description="Run the dashboard. Equivalent to `solisdash` with no subcommand.",
    )
    _add_run_args(run_p)

    add_p = subparsers.add_parser(
        "add-user",
        help="Create an additional Solisdash account (prompts for password).",
        description=(
            "Create a Solisdash account in the configured MongoDB. Reads "
            "settings from `~/.config/solisdash/solisdash.toml` (which the "
            "in-browser setup wizard writes) or env vars. The very first "
            "account is created from the browser-side wizard — this command "
            "is for adding more users later."
        ),
    )
    add_p.add_argument(
        "--username", required=True, help="Username for the new account."
    )
    add_p.add_argument(
        "--role",
        default="user",
        choices=("admin", "user"),
        help="Account role (default: user).",
    )

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _make_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "add-user":
        return cmd_add_user(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
