# Solisdash — initial build plan

Drafted 2026-05-13. Verify before implementing.

## Decided shape

- **Hybrid view:** live tiles on top, history charts below.
- **Audience:** shared instance, simple login, multiple SolisCloud accounts mapped to users. Not public.
- **Data flow:** live tiles call SolisCloud (short cache); historical charts read MongoDB, populated by a scheduled poller.
- **Stack:** FastAPI + HTMX + Jinja2, MongoDB (`solisdash-oat` for dev), uv, invoke. Chart.js for charts.

## Build sequence (one feature branch per group)

### 1. Scaffold ✅ (landed 2026-05-13, direct on `main` — first commit pending user approval)
- [x] `pyproject.toml` (uv-managed), `.python-version` (3.12), `uv.lock`.
- [x] `src/solisdash/__init__.py` with `__version__ = "0.1.0"`.
- [x] `src/solisdash/app.py` with FastAPI app + `/health`.
- [x] `tasks.py` with `start` / `stop` / `restart` / `status` / `test` / `lint` (uvicorn detached, pidfile in `var/uvicorn.pid`, logs in `var/uvicorn.log`).
- [x] `tests/` with `conftest.py` (TestClient fixture) and `test_smoke.py`. 2/2 passing under `pytest-xdist -n auto`.
- [x] `.env.example` listing `SOLIS_KEY_ID`, `SOLIS_KEYSECRET`, `SOLIS_API_URL`, `SOLIS_MONGODB_URI`, `SESSION_SECRET` (real `.env` already has the first four).
- [x] `.gitignore` (includes `.env`, `var/`, `.venv/`, caches), `README.md`.
- [x] `git init` (commit pending user approval).
- [ ] Mongo-per-worker fixture — deferred to the `storage` branch (no DB code yet).

### 2. SolisCloud client (`solis-client` branch) — the foundation
- [x] `src/solisdash/signing.py` — HMAC-SHA1 canonical-string builder, MD5-base64 body digest, RFC1123 Date. Flattened out of the now-removed `solis/` subpackage — everything in this project talks to SolisCloud, so the extra namespace was redundant.
- [x] Tests pinned to V2.0.3 §2.4 worked example: body `{"pageNo":1,"pageSize":10}` → Content-MD5 `kxdxk7rbAsrzSIWgEwhH4w==`, Date `Fri, 26 Jul 2019 06:00:46 GMT`. (Spec doesn't disclose the apiSecret used in §2.4's sig, so HMAC step is pinned to an independent stable vector.)
- [x] `src/solisdash/client.py` — async `httpx` client. Async context manager around one `httpx.AsyncClient`; exposes `user_station_list`, `station_detail`, `inverter_list`, `inverter_detail`, `station_day/month/year/all`, `alarm_list`. List responses come back as a `Page` dataclass (records + total + size + current + pages). KeyId header added alongside the four signed headers.
- [x] `SolisAPIError(code, msg)` raised on non-success envelope. Retryable codes `1004` / `1007` and HTTP `429`/`502`/`503`/`504` trigger exponential backoff + jitter, bounded by `max_retries` (default 3), with a pluggable `sleep` callable for tests. Non-retryable codes raise immediately.
- [x] List endpoints (`user_station_list`, `inverter_list`, `alarm_list`) take explicit `pageNo` / `pageSize` — no hidden auto-pagination.

### 3. Persistence (`storage` branch) — shared-key v1
- [x] `src/solisdash/db.py` — `connect()`, `get_database()`, `ensure_indexes()`, `INDEXES` map. Async via `pymongo.AsyncMongoClient` (pymongo 4.17, no motor).
- [x] Collections + indexes wired in `INDEXES`: `users` (unique `username`), `stations` (unique `id`), `station_samples` (`(station_id, ts)`), `station_daily` (unique `(station_id, date)`), `station_monthly` (unique `(station_id, month)`), `alarms` (`(station_id, alarm_begin_time desc)`, `state`).
- [x] Mongo-per-worker test fixture (`tests/conftest.py`): per-test `AsyncMongoClient` (pymongo binds to the loop it's opened on, so session-scoped clients clash with pytest-asyncio's per-test loops); per-worker DB `solis_test_<gw>`; `clean_db` fixture clears all known collections per test; `pytest_sessionfinish` drops the test DB at session end. Skips cleanly when `SOLIS_MONGODB_URI` is unset.
- [x] `tests/test_db.py` — verifies expected index names, unique-flag correctness, `ensure_indexes` idempotency, duplicate-key rejection on each unique index, indexed time-range query on `station_samples` (asserts `IXSCAN` via `explain`), `alarms.state` filter.
- [ ] **Deferred to a later branch:** `solis_accounts` for per-user keys. v1 reads `SOLIS_KEY_ID` / `SOLIS_KEYSECRET` / `SOLIS_API_URL` from `.env` and shares one SolisCloud account across all logged-in users.

### 4. Auth + shell (`auth-shell` branch)
- [x] Session-cookie login via `starlette.middleware.sessions.SessionMiddleware` (signed cookie, no server-side state). `bcrypt>=4.2` for hashing in `src/solisdash/auth.py` (hash/verify, `create_user`, `find_user`, `authenticate`, `session_login` / `session_logout`).
- [x] FastAPI dependencies `get_current_user` (optional) and `require_user` (mandatory — HTML clients get 303 → /login, API/HTMX get 401).
- [x] `src/solisdash/config.py` — pydantic-settings facade for env vars (`SOLIS_*`, `SESSION_SECRET`, `SOLIS_MONGODB_DB`); `lru_cache`d.
- [x] `src/solisdash/app.py` rewrite: lifespan (closes Mongo on shutdown), SessionMiddleware, lazy `get_db` dependency, `/login` GET+POST, `/logout`, `/` (auth-gated home), `/favicon.ico` (silences dev console), `/health` unchanged. Templates expose `version` via a global so every page footers correctly.
- [x] Pico CSS via CDN + `src/solisdash/static/style.css` for overrides; Jinja templates `base.html` / `login.html` / `home.html`.
- [x] `invoke add-user --username <name> --role <admin|user>` — prompts for password via `getpass`, ensures indexes, refuses unknown roles, surfaces `DuplicateKeyError` as a clean exit-1.
- [x] Tests (60 total, +23 in this branch): `tests/test_auth.py` (hash/verify, salt-per-call, create/find/authenticate, role validation, duplicate rejection); `tests/test_app_auth.py` (login form renders, unauthed → 303, JSON/HTMX → 401, login good/bad/unknown, full login→home→logout cycle, already-authed → redirect away from /login).
- [x] Test fixture `auth_client` uses `httpx.AsyncClient` over `ASGITransport` instead of sync `TestClient` so request handling shares the test's event loop (pymongo's `AsyncMongoClient` is loop-bound). The `client` fixture remains sync for endpoints that don't touch Mongo.
- [x] Browser smoke (Playwright): login page renders with Pico, form submits, home page shows the signed-in user + role, Sign out button clears the session and returns to /login. No console errors.

### 5. Live tiles (`live-tiles` branch)
- [x] `src/solisdash/tiles.py` — `TilesData` dataclass, `parse_station_detail` + `from_sample` field-pluckers, async-safe `TTLCache` (locks per key, callers collapse onto one factory invocation), `LiveTilesService` composing `SolisClient` + `station_samples` for tiles + station-list lookups.
- [x] HTMX-polled tiles on the home page: current power, today's yield, this month's yield, battery SOC, open alarms. Initial values server-rendered in `home.html`; HTMX swaps `_tiles.html` every 30 s via `hx-get="/tiles"`.
- [x] Two TTL caches: 15 min for the `userStationList` lookup (which rarely changes), 45 s for tile data (so the page refreshing every 30 s never outpaces the 2 req/sec rate limit).
- [x] Graceful degradation: `LiveTilesService.get_tiles` catches retryable `SolisAPIError` (`1004`/`1007`) and falls back to the most recent `station_samples` doc with `stale=True` + a "rate limited" note. Endpoint catches every error and renders a friendly alert rather than 500-ing.
- [x] Station selection: pin via `SOLIS_STATION_ID` env var; otherwise the first station from `userStationList` is picked and cached for 15 minutes.
- [x] `app.py`: SolisClient + LiveTilesService lazy-init on app.state; lifespan closes them on shutdown. `get_solis_client` / `get_tiles_service` deps. New `GET /tiles` returns an HTML fragment; `GET /` includes the same fragment for server-side first paint.
- [x] HTMX served from CDN (`htmx.org@2.0.4`) with SRI integrity hash. Pico CSS continues to drive the look; new tile-grid styles in `static/style.css`.
- [x] Tests (+21, now 81 total): `tests/test_tiles.py` (TTL cache hit/miss/concurrency/no-cached-exceptions, parsing + fallback shapes, service with mocked SolisCloud and DB fallback); `tests/test_app_tiles.py` (home renders tile values inline, `/tiles` returns a layout-free fragment, auth-gated, no-station / rate-limited / unconfigured / stale-fallback all produce friendly alerts).
- [x] `tests/conftest.py`: `auth_client` now also overrides `get_tiles_service` with a `_NullTilesService` so auth-focused tests don't try to reach SolisCloud.
- [x] Browser smoke (Playwright): logged in as a seeded user, home page rendered with the friendly alert path active (the dev API key is currently returning 403 from SolisCloud — credentials/IP-allowlist issue, not a code bug; signing-helper tests still pinned to the PDF worked example). No console errors. Logout still returns to /login.

### 6. Poller (`poller` branch)
- [ ] `invoke poll-once` and `invoke backfill --from … --to …` — both Python, both argparse-compatible.
- [ ] In-process scheduler (APScheduler) started by the FastAPI lifespan: every N minutes pull `stationDetail` into `station_samples`; nightly pull `stationDay` for the previous day into `station_daily`.
- [ ] Single token-bucket sized for the shared SolisCloud key (becomes per-key when multi-account lands).

### 7. History charts (`history` branch)
- [ ] Chart endpoints return JSON only (Mongo reads, no SolisCloud calls on the chart path).
- [ ] Chart.js views: day (5-min granularity), month (daily totals), year (monthly totals), all-time.
- [ ] Date-range picker, station picker. (No account picker in v1.)

### 8. Polish (`polish` branch)
- [ ] Alarm feed page.
- [ ] CSV export of daily/monthly totals.
- [ ] Dark mode (CSS variables, no JS toggle library).
- [ ] Health endpoint + readiness check for the poller.

## Out of scope (for now)

- Public sign-up, billing, multi-tenant isolation beyond per-user account ownership.
- Mobile-native app.
- Push notifications / email alerts on faults (defer until alarm feed exists).
- Forecasting / ML.

## Open questions before kickoff

- [ ] Confirm Python version (default 3.12 — fine?).
- [ ] Confirm hosting target — local laptop only, or a small VPS / home server? Affects whether the poller needs to survive reboots (systemd unit) vs. just run alongside `invoke start`.
- [ ] Sanity-check `SOLIS_API_URL` value against the spec's regional list (EU `https://www.soliscloud.com:13333` vs others).

## Review

_To be filled in as work lands._
