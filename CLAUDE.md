# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**Solisdash** — a small web app that surfaces solar/inverter data from the SolisCloud (Ginlong Solis) cloud platform.

Shape, decided 2026-05-13:

- **View:** hybrid — live tiles (current power, today's yield, battery SOC) at the top, historical charts (day/week/month/year) below.
- **Audience:** "me plus a few people" — shared instance, simple login, **one shared SolisCloud key for v1** (per-user keys deferred). Not public/multi-tenant; no sign-up flow.
- **Data flow:** hybrid — a scheduled poller pulls history into MongoDB, and the live tiles call SolisCloud directly (through a short server-side cache) so the dashboard stays useful during SolisCloud outages.

## Environment

`.env` at the repo root holds the SolisCloud credentials. Never read its **values** into transcripts or logs; reference variable names only.

| Variable | Purpose |
| --- | --- |
| `SOLIS_KEY_ID` | SolisCloud API Key ID (the `apiId` in the `Authorization` header). |
| `SOLIS_KEYSECRET` | SolisCloud API Key Secret (HMAC-SHA1 signing key). One word — no underscore between `KEY` and `SECRET`. |
| `SOLIS_API_URL` | Region-specific base URL, e.g. `https://www.soliscloud.com:13333`. |
| `SOLIS_MONGODB_URI` | MongoDB Atlas connection string (`mongodb+srv://…`). Database name is **`solis`**. Local dev must point at a non-prod cluster. |
| `SOLIS_MONGODB_DB` | Database name (default `solis`). Override for sandbox runs. |
| `SOLIS_STATION_ID` | *Optional* — pin the dashboard to one station. Empty = pick the first one returned by `userStationList`. |
| `SESSION_SECRET` | Session-cookie signing key. Required in production. |
| `RUN_SCHEDULER` | *Default `false`*. Set `true` to run the in-process poller alongside uvicorn (live tiles work without it). |
| `SCHEDULER_SAMPLE_MINUTES` | How often the scheduler pulls `stationDetail` into `station_samples`. Default 5. |
| `SCHEDULER_DAILY_HOUR_UTC` / `SCHEDULER_DAILY_MINUTE_UTC` | When the daily rollup runs. Default 00:30 UTC. |
| `SCHEDULER_RATE_PER_SEC` | Outbound rate-limit for the poller's token bucket. Default 1.5 (SolisCloud's per-endpoint cap is 2). |

`.gitignore` lists `.env` so it can't be committed. See `.env.example` for the full set.

Hosting: production runs on a **Digital Ocean droplet** (single uvicorn process under systemd, planned). Local dev runs the same `invoke start` task. The poller is in-process under uvicorn's lifespan (APScheduler) — no separate poller service.

## Source-of-truth spec

`SolisCloud Platform API Document V2.0.3.pdf` — the upstream API spec. Read it before implementing any new endpoint binding.

Things the spec dictates (verify in the PDF before relying on them in code):

- **Auth is per-request HMAC-SHA1.** Required headers: `Date`, `Content-MD5`, `Content-Type`, `Authorization` (the apiId travels inside Authorization — no separate `KeyId` header, despite the PDF mentioning one in passing). The signing string is a canonicalised concatenation of method + Content-MD5 + Content-Type + Date + resource path. **Content-Type must be bare `application/json`**, not `application/json;charset=UTF-8` — the §2.2 prose contradicts §2.4's worked example, and the live API returns 403 `wrong sign` if you send the charset suffix. Get the signing helper right once and unit-test it against the worked examples in the PDF — most integration bugs live here.
- **Base URL is region-specific.** Pick it from the PDF, do not hard-code the EU host.
- **Rate limits are strict and per-key.** Client must back off on `1004` / `1007`-class errors instead of tight retries. The poller cadence has to respect this — assume minute-granularity at best for "live" calls, lower for history.
- **Response envelope** is `{code, msg, data, success}`. Surface the upstream `code` in any raised exception so callers can branch on it.

## Tech stack (defaults inherited from `~/CLAUDE.md` — do not improvise alternatives)

- **Python + uv** for everything (`uv run python -m …`, `uv sync`, `uv pip install`).
- **FastAPI** for the web app.
- **MongoDB Atlas** for persistence. Connection string read from `SOLIS_MONGODB_URI` (`mongodb+srv://…`). Database name is **`solis`**. Local dev must point at a non-prod Atlas cluster — **never** the production cluster.
- **HTMX + Jinja2** for the frontend. React is forbidden by global rules; HTMX fits FastAPI's server-rendered model and keeps the JS surface tiny. Chart.js (vendored or CDN) for historical charts.
- **invoke** for build/admin tasks — including `start` / `stop` / `restart` of the FastAPI app, `poll-once`, `backfill`, `add-user`, `add-solis-account`.
- **argparse** for any standalone CLI scripts.
- Type hints on every function and class.

## Commands

All Python invocations go through `uv run` — bare `pytest` / `invoke` can be intercepted by pyenv.

- `uv sync` — install/refresh dependencies (after editing `pyproject.toml`).
- `uv run python -m invoke start` — start app under uvicorn, detached, pidfile in `var/uvicorn.pid`, logs to `var/uvicorn.log`. Add `--reload` for dev autoreload.
- `uv run python -m invoke stop` / `restart` / `status` — lifecycle and `/health` probe.
- `uv run python -m invoke test` — full pytest suite in parallel (`-n auto`).
- `uv run python -m invoke lint` — ruff + mypy.
- Single test: `uv run python -m pytest tests/test_smoke.py::test_health -q` (or pass `-k <expr>`).

## Build order

The scaffold step (steps 1–2 below) has already landed. Subsequent feature work follows the sequence in `tasks/todo.md` — do not skip ahead:

1. ~~Plan in `tasks/todo.md`.~~
2. ~~Scaffold `pyproject.toml`, `.python-version`, `tasks.py`, `src/solisdash/`, `tests/`.~~
3. **SolisCloud auth/signing helper first**, with regression tests pinned to the worked examples in the V2.0.3 PDF — every later piece depends on it.
4. Then station/inverter list endpoints, then live tiles, then poller + history.

Develop on a feature branch in a git worktree (`git worktree add ../solisdash-<branch> -b <branch>`), never directly on `main`. Bump the version in `pyproject.toml` and `src/solisdash/__init__.py` on every push (keep the two in sync).

## SolisCloud concepts (vocabulary cheat-sheet)

These names recur in the API and should be used as-is in code/UI to keep grep-ability with the spec:

- **Station** — a physical site (one address, one owner). Has an `id` and an `userId`.
- **Inverter** — a device under a station, identified by `sn` (serial number).
- **Collector** — the data-logger that ships inverter telemetry to SolisCloud.
- **Battery / EPM / Meter** — accessory devices reported alongside the inverter when present.

Common endpoints the dashboard will lean on (verify paths against the PDF):

- `/v1/api/userStationList` — paginated station list for a user-key.
- `/v1/api/stationDetail` — current state for one station (live tiles).
- `/v1/api/inverterList`, `/v1/api/inverterDetail` — per-inverter drill-down.
- `/v1/api/stationDay` / `stationMonth` / `stationYear` / `stationAll` — historical energy series for charts.
- `/v1/api/alarmList` — fault/alarm feed.

## Notes

- Update sections of this file with `file:line` references once the corresponding modules land — replace prose with pointers.
- If anything in the live code contradicts a note here, trust the code and fix the note.
