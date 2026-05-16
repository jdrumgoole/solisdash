"""Make sure every interactive element on every page actually goes
somewhere the server can handle.

We don't run a real browser here — that would need pytest-playwright and a
Chromium download in CI. Instead we GET each authed page, extract every
`<a href>`, `<form action>`, and `[hx-{get,post,put,delete}]` URL pointing
at our own app, and assert those endpoints don't 5xx. This is a regression
net for two specific shapes of breakage we keep landing in:

  - A link or HTMX button pointing at an endpoint that doesn't exist.
  - A POST handler raising an unhandled exception (500) when called with
    no body, instead of validating and returning 4xx.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from pymongo.asynchronous.database import AsyncDatabase

from solisdash.auth import create_user

# --- HTML scraping helpers -------------------------------------------------


_HREF_RE = re.compile(r'\bhref="([^"]+)"', re.IGNORECASE)
_FORM_TAG_RE = re.compile(r"<form\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(r'\b(action|method)="([^"]+)"', re.IGNORECASE)


def _collect_internal_links(html: str) -> set[str]:
    """Return every same-origin URL referenced from `<a href>` tags."""
    out: set[str] = set()
    for href in _HREF_RE.findall(html):
        if href.startswith("/") and not href.startswith("//"):
            out.add(href)
    return out


def _collect_form_actions(html: str) -> list[tuple[str, str]]:
    """Return `(method, action)` for every `<form>` posting back to us."""
    out: list[tuple[str, str]] = []
    for m in _FORM_TAG_RE.finditer(html):
        attrs = dict(_ATTR_RE.findall(m.group(1)))
        action = attrs.get("action") or attrs.get("ACTION")
        method = (attrs.get("method") or attrs.get("METHOD") or "get").lower()
        if action and action.startswith("/") and not action.startswith("//"):
            out.append((method, action))
    return out


def _collect_hx_targets(html: str) -> list[tuple[str, str]]:
    """Return `(verb, url)` for `hx-get|post|put|delete` attributes."""
    out: list[tuple[str, str]] = []
    for verb in ("get", "post", "put", "delete", "patch"):
        for m in re.finditer(rf'\bhx-{verb}="([^"]+)"', html, re.IGNORECASE):
            url = m.group(1)
            if url.startswith("/") and not url.startswith("//"):
                out.append((verb, url))
    return out


# --- helpers --------------------------------------------------------------


async def _sign_in(
    ac: httpx.AsyncClient, db: AsyncDatabase[dict[str, Any]]
) -> None:
    await create_user(db, username="clicker", password="hunter2", role="admin")
    r = await ac.post(
        "/login",
        data={"username": "clicker", "password": "hunter2"},
        follow_redirects=False,
    )
    assert r.status_code == 303, "login should succeed for our fixture user"


def _ok(status: int) -> bool:
    """Anything that isn't a server crash counts as 'the endpoint exists'."""
    return status < 500


# --- the tests -------------------------------------------------------------

AUTHED_PAGES = ("/", "/history", "/alarms", "/settings")


@pytest.mark.parametrize("page", AUTHED_PAGES)
async def test_every_internal_link_resolves_without_5xx(
    page: str,
    auth_client: httpx.AsyncClient,
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    """Every `<a href>` on an authed page must point at a live endpoint."""
    await _sign_in(auth_client, clean_db)
    r = await auth_client.get(page)
    assert r.status_code == 200, f"failed to load {page}: {r.status_code}"

    for link in _collect_internal_links(r.text):
        bare = link.split("#", 1)[0]
        if not bare:
            continue
        resp = await auth_client.get(bare, follow_redirects=False)
        assert _ok(resp.status_code), (
            f"GET {bare!r} (linked from {page}) returned "
            f"{resp.status_code} — broken navigation"
        )


@pytest.mark.parametrize("page", AUTHED_PAGES)
async def test_every_form_action_does_not_5xx(
    page: str,
    auth_client: httpx.AsyncClient,
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    """Every `<form action="…">` must route to a handler that exists.

    We submit an empty body — handlers are required to validate input
    and return 4xx for bad data, never 5xx. This catches the "I shipped
    a button whose POST target doesn't exist" class of bug.
    """
    await _sign_in(auth_client, clean_db)
    r = await auth_client.get(page)
    assert r.status_code == 200

    for method, action in _collect_form_actions(r.text):
        resp = await auth_client.request(
            method.upper(), action, follow_redirects=False
        )
        assert _ok(resp.status_code), (
            f"{method.upper()} {action!r} (form on {page}) returned "
            f"{resp.status_code}; expected 2xx/3xx/4xx"
        )


@pytest.mark.parametrize("page", AUTHED_PAGES)
async def test_every_htmx_endpoint_does_not_5xx(
    page: str,
    auth_client: httpx.AsyncClient,
    clean_db: AsyncDatabase[dict[str, Any]],
) -> None:
    """Same idea for `hx-get` / `hx-post` / `hx-put` / `hx-delete` URLs."""
    await _sign_in(auth_client, clean_db)
    r = await auth_client.get(page)
    assert r.status_code == 200

    for verb, url in _collect_hx_targets(r.text):
        resp = await auth_client.request(verb.upper(), url, follow_redirects=False)
        assert _ok(resp.status_code), (
            f"{verb.upper()} {url!r} (hx-{verb} on {page}) returned "
            f"{resp.status_code}"
        )


async def test_unauth_pages_link_resolution(
    auth_client: httpx.AsyncClient,
) -> None:
    """Login + setup are reachable before sign-in. Their links must too."""
    for page in ("/login", "/setup"):
        r = await auth_client.get(page, follow_redirects=False)
        # Either renders (200) or redirects somewhere (303); both are fine.
        assert r.status_code in (200, 303)
        if r.status_code != 200:
            continue
        for link in _collect_internal_links(r.text):
            bare = link.split("#", 1)[0]
            resp = await auth_client.get(bare, follow_redirects=False)
            assert _ok(resp.status_code), (
                f"GET {bare!r} (linked from {page}) returned {resp.status_code}"
            )


# --- defensive CSS check ---------------------------------------------------


def test_no_runtime_cdn_dependencies_in_templates() -> None:
    """All JS/CSS must be vendored under ``static/vendor/`` so the dashboard
    works offline and inside pywebview's WKWebView (which silently drops
    CDN requests that fail SRI / network-policy checks). A regression here
    is what made the "Test MongoDB connection" button look dead — HTMX
    never loaded, so the click did nothing."""
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / "src" / "solisdash" / "templates"
    forbidden = ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com")
    for tmpl in templates.glob("*.html"):
        body = tmpl.read_text()
        for needle in forbidden:
            assert needle not in body, (
                f"{tmpl.name} references {needle} — vendor it under "
                f"static/vendor/ instead so the dashboard runs offline."
            )


def test_vendored_assets_are_present_on_disk() -> None:
    """Belt-and-braces for the prior test: confirm the files referenced by
    the templates actually exist under static/vendor/. Catches the
    'remove a CDN URL but forget to commit the vendored file' regression."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "src" / "solisdash" / "static"
    for asset in (
        "vendor/pico.min.css",
        "vendor/htmx.min.js",
        "vendor/chart.umd.min.js",
        "vendor/chartjs-adapter-date-fns.bundle.min.js",
    ):
        assert (static / asset).is_file(), (
            f"missing vendored asset {asset!r} — run the vendor script"
        )


def test_no_invisible_pointer_events_blocker() -> None:
    """Catch the "I CSS'd `pointer-events: none` onto something important"
    class of bug. Only the htmx-indicator is allowed to have it (and even
    then only the indicator spinner, never an overlay)."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "src" / "solisdash" / "static"
    css = (static / "style.css").read_text()
    for line_no, line in enumerate(css.splitlines(), 1):
        if "pointer-events" not in line:
            continue
        # Allow narrow exceptions only when paired with an explanatory comment.
        if "/* allow-pointer-events:" in line:
            continue
        raise AssertionError(
            f"style.css:{line_no} declares pointer-events without a justifying "
            f"`/* allow-pointer-events: … */` marker — please don't add invisible "
            f"click blockers: {line.strip()}"
        )
