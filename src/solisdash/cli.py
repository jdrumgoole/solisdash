"""`solisdash` CLI: run the dashboard, or manage users.

Subcommands:
- (no subcommand) / `run` — open the dashboard inside a pywebview window
- `add-user`             — seed an account (the only path before login works)

Pure-Python entry point installed by the wheel. No `uv` / `invoke` /
external tooling at runtime — just `pip install solisdash && solisdash`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
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
DEFAULT_MONGO_URI = "mongodb://localhost:27017/"
DEFAULT_API_URL = "https://www.soliscloud.com:13333"


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
    # pywebview 6.x takes the app icon on `start()`, not `create_window()` —
    # it's the dock / taskbar icon, set once per process.
    start_kwargs: dict[str, object] = {"menu": _build_menu(base_url), "debug": debug}
    if icon is not None:
        start_kwargs["icon"] = icon
    webview.start(**start_kwargs)  # type: ignore[arg-type]


# --- subcommand: run -------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        if interactive_setup() == "aborted":
            return 2
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        return 130
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


# --- first-run interactive onboarding --------------------------------------


def _is_tty() -> bool:
    """True when both stdin and stdout are connected to a real terminal.

    The onboarding prompts only fire when both ends are interactive — under
    systemd / ssh-no-tty / CI we bail with a friendly message instead.
    """
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt(
    label: str,
    *,
    default: str = "",
    secret: bool = False,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
) -> str:
    """One labelled prompt with optional default and secret-input handling.

    `input_fn` / `getpass_fn` are injected so tests can stub them.
    """
    prompt = f"  {label}"
    if default and not secret:
        prompt += f" [{default}]"
    prompt += ": "
    raw = getpass_fn(prompt) if secret else input_fn(prompt).strip()
    return raw or default


def _read_kv(path: Path) -> dict[str, str]:
    """Parse a simple `.env`-style file. Empty / comment lines are skipped."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def _write_kv(path: Path, updates: dict[str, str]) -> None:
    """Merge `updates` into `path` and write atomically.

    Existing comments / formatting are not preserved — this is the first-run
    writer; if the user later hand-edits the file we leave it alone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _read_kv(path)
    merged.update({k: v for k, v in updates.items() if v})
    body = "\n".join(f"{k}={v}" for k, v in merged.items())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    tmp.replace(path)
    # Stash the secret away from group / other readers.
    try:
        path.chmod(0o600)
    except OSError:
        pass  # best-effort; Windows / unusual filesystems


def _apply_env(updates: dict[str, str]) -> None:
    """Update `os.environ` and clear the lru_cached settings."""
    from solisdash.config import get_settings

    for k, v in updates.items():
        if v:
            os.environ[k] = v
    get_settings.cache_clear()


def interactive_setup(
    *,
    is_tty_fn: Callable[[], bool] = _is_tty,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    out: Callable[[str], None] = print,
) -> str:
    """Walk the user through first-run config when SOLIS_MONGODB_URI is missing.

    Returns one of:
    - `"skip"`      — nothing was missing, or non-TTY (server can still
                      come up; the web `/setup` wizard will handle it).
    - `"configured"`— prompts ran, values written to user config.
    - `"aborted"`   — non-TTY *and* missing Mongo. Caller should exit non-zero.
    """
    from solisdash.config import get_settings, user_config_path

    settings = get_settings()
    if settings.SOLIS_MONGODB_URI:
        # Already configured — silently fill in a SESSION_SECRET if missing.
        if not settings.SESSION_SECRET:
            secret_only = {"SESSION_SECRET": secrets.token_urlsafe(32)}
            _write_kv(user_config_path(), secret_only)
            _apply_env(secret_only)
            log.info(
                "Generated SESSION_SECRET and saved to %s", user_config_path()
            )
        return "skip"

    if not is_tty_fn():
        out(
            "Solisdash isn't configured yet and stdin isn't a terminal — "
            "can't prompt interactively. Set SOLIS_MONGODB_URI (and "
            "ideally SOLIS_KEY_ID / SOLIS_KEYSECRET / SESSION_SECRET) in "
            f"{user_config_path()} or via environment variables."
        )
        return "aborted"

    config_path = user_config_path()
    out("")
    out("Solisdash first-run setup.")
    out("Press Enter to accept the default in brackets. Values get saved")
    out(f"to {config_path}. Re-run `solisdash` to start with the saved config.")
    out("")

    updates: dict[str, str] = {}
    updates["SOLIS_MONGODB_URI"] = _prompt(
        "MongoDB connection URI",
        default=DEFAULT_MONGO_URI,
        input_fn=input_fn,
        getpass_fn=getpass_fn,
    )
    updates["SOLIS_API_URL"] = _prompt(
        "SolisCloud API URL",
        default=DEFAULT_API_URL,
        input_fn=input_fn,
        getpass_fn=getpass_fn,
    )
    updates["SOLIS_KEY_ID"] = _prompt(
        "SolisCloud Key ID (Account → Basic Settings → API Management, "
        "blank to skip and configure later)",
        input_fn=input_fn,
        getpass_fn=getpass_fn,
    )
    if updates["SOLIS_KEY_ID"]:
        updates["SOLIS_KEYSECRET"] = _prompt(
            "SolisCloud Key Secret",
            secret=True,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
        )
    updates["SESSION_SECRET"] = settings.SESSION_SECRET or secrets.token_urlsafe(32)

    _write_kv(config_path, updates)
    _apply_env(updates)
    out("")
    out(f"Saved to {config_path}")
    return "configured"


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
        print("SOLIS_MONGODB_URI is not set", file=sys.stderr)
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
            "native pywebview window."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"solisdash {__version__}",
    )
    # The run-mode flags are also accepted at the top level so
    # `solisdash --port 9000` keeps working without an explicit subcommand.
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
        help="Create a Solisdash account (prompts for password).",
        description=(
            "Create a Solisdash account in the configured MongoDB. Reads "
            "SOLIS_MONGODB_URI from environment / .env. Prompts for the "
            "password — it's never accepted on the command line."
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
    # Both `solisdash` and `solisdash run` end up here.
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
