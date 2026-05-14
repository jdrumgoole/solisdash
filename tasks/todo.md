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
- [ ] Session-cookie login, `bcrypt` password hashing.
- [ ] `invoke add-user --username … --role …` to seed the first account.
- [ ] Base Jinja layout, top nav, error pages, sign-out. (No account switcher in v1 — shared key.)

### 5. Live tiles (`live-tiles` branch)
- [ ] HTMX-polled tiles: current power, today's yield, this-month yield, battery SOC, alarm count.
- [ ] In-process LRU/TTL cache (30–60 s) keyed by `station_id` so a refresh storm doesn't blow the SolisCloud rate limit.
- [ ] Tiles degrade gracefully to "last known" from `station_samples` when SolisCloud returns rate-limit codes.

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
