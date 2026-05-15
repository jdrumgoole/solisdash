from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from solisdash.cli import (
    UvicornServerThread,
    _icon_path,
    find_free_port,
    parse_args,
    wait_for_health,
)

# --- port + arg parsing ----------------------------------------------------


def test_find_free_port_returns_a_bindable_port() -> None:
    port = find_free_port()
    assert isinstance(port, int)
    assert 1024 < port < 65536
    # Confirm the port is actually free right now (race-y in principle).
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 0
    assert args.no_window is False
    assert args.debug is False
    assert args.timeout == pytest.approx(15.0)


def test_parse_args_supports_flags() -> None:
    args = parse_args(["--host", "0.0.0.0", "--port", "9000", "--no-window", "--debug"])
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.no_window is True
    assert args.debug is True


def test_parse_args_default_command_is_none() -> None:
    """Bare `solisdash` dispatches to the run command."""
    args = parse_args([])
    assert args.command is None


def test_parse_args_run_subcommand_accepts_run_flags() -> None:
    args = parse_args(["run", "--port", "9100"])
    assert args.command == "run"
    assert args.port == 9100


def test_parse_args_add_user_subcommand() -> None:
    args = parse_args(["add-user", "--username", "joe", "--role", "admin"])
    assert args.command == "add-user"
    assert args.username == "joe"
    assert args.role == "admin"


def test_parse_args_add_user_defaults_role_to_user() -> None:
    args = parse_args(["add-user", "--username", "joe"])
    assert args.role == "user"


def test_parse_args_add_user_rejects_unknown_role() -> None:
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        parse_args(["add-user", "--username", "joe", "--role", "wizard"])


# --- health waiter against a stand-in HTTP server -------------------------


class _HealthHandler(BaseHTTPRequestHandler):
    delay_until: float = 0.0

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        if time.monotonic() < self.delay_until:
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *_args: object) -> None:  # silence access logs
        return


def _spawn_health_server(*, delay_s: float = 0.0) -> tuple[HTTPServer, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    _HealthHandler.delay_until = time.monotonic() + delay_s
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, t


def test_wait_for_health_returns_once_endpoint_is_ready() -> None:
    server, _t = _spawn_health_server()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        wait_for_health(base, timeout_s=5.0)
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_health_raises_on_timeout() -> None:
    server, _t = _spawn_health_server(delay_s=10.0)  # always 503 within the test
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with pytest.raises(RuntimeError, match="did not become healthy"):
            wait_for_health(base, timeout_s=0.5)
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_health_raises_when_nothing_is_listening() -> None:
    # Bind a port then immediately release it — nothing's there to answer.
    port = find_free_port()
    with pytest.raises(RuntimeError, match="did not become healthy"):
        wait_for_health(f"http://127.0.0.1:{port}", timeout_s=0.5)


# --- icon discovery --------------------------------------------------------


def test_icon_path_resolves_to_a_real_png() -> None:
    """The CLI bundles a sun icon; this guards against build-time omissions."""
    import os

    path = _icon_path()
    assert path is not None, "src/solisdash/static/icon.png missing from package"
    assert os.path.exists(path)
    with open(path, "rb") as f:
        header = f.read(8)
    # PNG magic bytes.
    assert header == b"\x89PNG\r\n\x1a\n"


# --- UvicornServerThread can boot the real app and serve /health ----------


def test_uvicorn_server_thread_starts_and_serves_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end smoke for the threaded server the CLI uses."""
    # Provide a session secret so the app imports cleanly even if .env is absent.
    monkeypatch.setenv("SESSION_SECRET", "cli-test-only")

    port = find_free_port()
    server = UvicornServerThread(host="127.0.0.1", port=port, log_level="warning")
    server.start()
    try:
        wait_for_health(server.base_url, timeout_s=10.0)
    finally:
        server.stop()
        server.join(timeout=5.0)
    assert not server.is_alive()
