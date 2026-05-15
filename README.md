# Solisdash

[![CI](https://github.com/jdrumgoole/solisdash/actions/workflows/ci.yml/badge.svg)](https://github.com/jdrumgoole/solisdash/actions/workflows/ci.yml)

Self-hosted web dashboard for the [SolisCloud](https://www.soliscloud.com/) (Ginlong Solis) platform. Live tiles on top, historical charts and an alarm feed below. FastAPI + HTMX + Jinja2 + Pico CSS, MongoDB-backed, with an in-process APScheduler poller.

## What you get

- **Live tiles** — current power, today's yield, this-month yield, battery SOC, open alarms. Refreshed every 30 s via HTMX; falls back to the most recent stored sample when SolisCloud rate-limits.
- **History charts** — day (5-min granularity), month (daily totals), year (monthly totals), all-time (yearly totals). Reads MongoDB only, no upstream calls on the chart path.
- **CSV export** for any chart view.
- **Alarm feed** paginated and filterable by station + state.
- **In-process poller** (APScheduler) that pulls `stationDetail` every 5 min and alarms on the same cadence, plus a nightly daily-rollup job at 00:30 UTC. Rate-limited via a shared `TokenBucket`.
- **Session-cookie auth** (bcrypt) with an `invoke add-user` CLI.
- **`/health`** (liveness) and **`/ready`** (Mongo + scheduler) probes.
- **Dark-mode toggle** that persists per-browser.

## Quick start

1. `cp .env.example .env` and fill in:
   - `SOLIS_KEY_ID` / `SOLIS_KEYSECRET` from SolisCloud → Account → Basic Settings → API Management.
   - `SOLIS_API_URL` — the region-specific base URL (EU: `https://www.soliscloud.com:13333`).
   - `SOLIS_MONGODB_URI` — a non-prod MongoDB Atlas cluster.
   - `SESSION_SECRET` — generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
   - Optional: `RUN_SCHEDULER=true` to enable the in-process poller, `SOLIS_STATION_ID` to pin the dashboard to one station.
2. `uv sync --extra dev`
3. Seed your first admin: `uv run python -m invoke add-user --username you --role admin`
4. `uv run python -m invoke start` — runs detached, log at `var/uvicorn.log`, lifecycle via `stop` / `restart` / `status`.
5. Visit http://127.0.0.1:8000.

## Pages

| Path | Notes |
| --- | --- |
| `/` | Live tile dashboard (auth-gated). |
| `/history` | Day / month / year / all-time charts with a download-CSV link. |
| `/alarms` | Filterable alarm feed (auth-gated). |
| `/login`, `/logout` | Session cookie auth. |
| `/health`, `/ready` | Liveness + readiness probes. |

## Invoke tasks

```
uv run python -m invoke start           # detached uvicorn
uv run python -m invoke stop / restart / status
uv run python -m invoke test            # full suite under pytest-xdist
uv run python -m invoke lint            # ruff + mypy
uv run python -m invoke add-user --username … --role admin|user
uv run python -m invoke poll-once       # one-shot stationDetail pull
uv run python -m invoke backfill --start YYYY-MM-DD --end YYYY-MM-DD
```

## Layout

- `src/solisdash/` — application package
  - `app.py` — FastAPI app, lifespan, routes
  - `client.py` — async SolisCloud client (HMAC-SHA1 signing, retry/backoff)
  - `signing.py` — HMAC-SHA1 signing helper, pinned to the V2.0.3 worked example
  - `db.py` — Mongo connection + index schema
  - `auth.py` — bcrypt hashing, session helpers, FastAPI deps
  - `config.py` — pydantic-settings facade
  - `tiles.py`, `history.py`, `alarms.py` — page-specific services
  - `poller.py`, `ratelimit.py`, `scheduler.py` — APScheduler poller + token bucket
  - `templates/`, `static/` — Jinja2 + Pico CSS
- `tests/` — pytest suite (160 tests, parallel-safe under `pytest-xdist`)
- `tasks.py` — invoke build/admin tasks
- `tasks/todo.md` — build plan (every step ticked through Polish)
- `CLAUDE.md` — project conventions for Claude Code sessions
- `SolisCloud Platform API Document V2.0.3.pdf` — upstream API spec (source of truth)

## Development

```
uv run python -m invoke lint    # ruff + mypy
uv run python -m invoke test    # pytest -n auto
```

CI runs the same against a `mongo:7` service container in GitHub Actions on every push and PR to `main`.

## License

[GNU Affero General Public License v3.0 or later](LICENSE) (AGPL-3.0-or-later). Network use is distribution — if you run a modified version of Solisdash on a server users interact with, they're entitled to the modified source.
