# Changelog

All notable changes to **Solisdash** are documented here. Version numbers
follow [Semantic Versioning](https://semver.org/). The package is published
to PyPI on every `vX.Y.Z` tag push.

## 0.7.2 — 2026-05-16

### Changed
- The History page's empty state no longer tells the user to drop to a
  shell and run `uv run python -m invoke poll-once`. It now offers a
  **Poll SolisCloud now** button that pulls each station's current state
  once via a new auth-gated `POST /history/poll-now` endpoint and
  refreshes the page so the chart UI takes over. Keeps the
  client experience GUI-only.

### Fixed
- `get_solis_client` no longer raises `AttributeError` when the
  lifespan handler hasn't initialised `app.state` (e.g. under
  `httpx.ASGITransport` in tests).

## 0.7.1 — 2026-05-16

### Fixed
- Submitting the setup wizard against a MongoDB that already contained a
  user with the chosen username raised `DuplicateKeyError` and returned a
  500. The wizard now probes for a username collision before persisting
  anything and renders a friendly inline error pointing the user at
  `/login`. A `DuplicateKeyError` fallback covers the index race.

## 0.7.0 — 2026-05-16

### Changed
- All configuration now lives inside the webview. The CLI no longer prompts
  for anything — `solisdash` boots straight into the pywebview window and
  silently generates `SESSION_SECRET` on first run.

### Added
- First-run **setup wizard** at `/setup`: a single-page form with three
  sections (MongoDB, SolisCloud, Administrator account). Each section has
  an HTMX **Test** button that probes the value inline before save. After
  save the user is auto-signed-in and dropped on the dashboard.
- **Settings page** at `/settings` (linked from the nav as ⚙ Settings):
  re-edit the same values, re-test, or reset the configuration. Reset
  wipes `solisdash.toml` and re-runs the wizard; MongoDB data
  (users, samples, alarms, daily totals) is left alone.
- **`~/.config/solisdash/solisdash.toml`** as the persisted config store,
  XDG-aware and chmodded to `0600`. Loaded via pydantic-settings'
  `TomlConfigSettingsSource`. Environment variables still take precedence
  over the file, and a project-local `.env` is still read for dev.

### Removed
- The interactive CLI setup helpers (`solisdash setup`, prompts on first
  boot). The `add-user` subcommand stays for post-install user management.

## 0.6.2 — 2026-05-15

### Changed
- Dropped `invoke` as a runtime dependency — it was unused in `src/` and
  shipped only because the build tasks lived in the package. Now in the
  `dev` extra.

## 0.6.1 — 2026-05-15

### Fixed
- Signing in on a freshly-installed instance with empty SolisCloud
  credentials raised `RuntimeError` from `get_solis_client`, producing a
  500. The client now constructs even with empty creds; live-tile
  requests fall back to a friendly "SolisCloud rejected the call" alert
  that nudges the admin to finish configuration.

## 0.6.0 — 2026-05-15

### Added
- First-run onboarding: when no `.env` is present and stdin is a TTY,
  the CLI prompts the admin to enter Mongo + SolisCloud creds and writes
  them to a user-level config file. Superseded by the in-browser wizard
  in 0.7.0.

## 0.5.0 — 2026-05-15

### Added
- First-run setup wizard at `/setup` (server-side, no Mongo creds yet)
  so a fresh install can create its admin account from the browser
  instead of the CLI.
- `solisdash add-user` CLI subcommand for creating additional accounts
  post-install.

## 0.4.0 — 2026-05-15

### Changed
- Lowered the minimum Python version from 3.12 to **3.10** so the
  package installs on the broader range of LTS Pythons. `tomli` is
  pulled in as a fallback for `tomllib` on 3.10.

## 0.3.2 — 2026-05-15

### Removed
- The obsolete `pymongo[srv]` extra. Recent `pymongo` releases bundle
  SRV resolution by default; the extra was a no-op causing a pip
  warning.

## 0.3.1 — 2026-05-15

### Added
- GitHub Actions release workflow (`.github/workflows/release.yml`):
  pushing a `vX.Y.Z` tag builds with `uv build` and publishes the sdist
  + wheel to PyPI via OIDC trusted publishing. No API tokens, no local
  `uv publish`.

## 0.2.x — 2026-05-13 → 2026-05-14

Pre-release scaffolding. Highlights from the run-up to the first
PyPI-published version:

- `solisdash` CLI launcher with native pywebview window + menu.
- AGPL-3.0-or-later license.
- GitHub Actions CI (ruff, mypy, pytest with a `mongo:7` service).
- Public README and CI badge.

## 0.1.x — 2026-05-13

Initial development. Highlights:

- FastAPI app scaffold, session-cookie auth, Jinja shell.
- SolisCloud client with HMAC-SHA1 signing (bare `application/json`
  Content-Type, no separate `KeyId` header, `alarmList` unwrapping
  fixed).
- MongoDB layer with per-worker test fixture.
- Live-tile dashboard with HTMX polling and station-samples fallback.
- Poller + APScheduler + token-bucket rate limiter, with `poll-once`
  and `backfill` invoke tasks.
- History charts: day, month, year, all-time (Mongo-only, no SolisCloud
  calls).
- Alarm feed, CSV export, dark-mode toggle, `/ready` probe.
