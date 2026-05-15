"""`solisdash` CLI: run the FastAPI app inside a native pywebview window.

The CLI is for "desktop app" runs on the developer's machine. For a
real deployment use `uv run python -m invoke start`; for tests use
`pytest`. This entry point keeps the same FastAPI app, just hides
the browser shell behind a Cocoa/GTK/EdgeWebView2 window with a
native menu and the sun icon.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import time
import webbrowser
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="solisdash",
        description="Run Solisdash inside a native window (pywebview).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Address uvicorn binds to. Default: 127.0.0.1 (loopback only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to bind. 0 (default) picks a free one.",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="Run uvicorn without opening pywebview (useful for ssh / headless smoke).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Open pywebview's dev tools and use uvicorn debug logging.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"solisdash {__version__}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for the app to become healthy before giving up.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
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


if __name__ == "__main__":
    sys.exit(main())
