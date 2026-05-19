# Changelog

All notable changes to **Solisdash** are documented here. Version numbers
follow [Semantic Versioning](https://semver.org/). The package is published
to PyPI on every `vX.Y.Z` tag push.

## 0.10.0 — 2026-05-19

A big iteration on the History page: more metrics, richer capture, and a
configurable financial view. Headline changes:

### Added
- **Intraday backfill via `stationDay`.** Every Fetch / Poll-now run
  now also pulls the per-5-minute SolisCloud curve for each date in
  range and upserts it into `station_samples`. Historical days you
  weren't running the scheduler over get the full minute-grained shape
  back, not just daily totals.
- **Full transient capture.** Each `station_samples` row stores the
  entire upstream `stationDetail` payload under `raw`. That's ~375
  fields of point-in-time data per minute (instantaneous family load,
  running grid purchase/sell, daily income, CO₂ avoided, full-load
  hours, battery direction, etc.) so nothing is silently dropped
  between polls.
- **`/data` tab** consolidating Poll, Fetch (date-range backfill with
  live progress bar), and Purge — all moved off the History / Settings
  pages.
- **Dashboard home charts.** Three live panels under the tile strip:
  Today's power, Battery SOC (7 days), This month's daily energy.
  Auto-refresh every 60s, paused when the tab is hidden.
- **Charge / Discharge / Consumption / Import / Export / Net
  metrics.** Five new History-page tabs powered by fields newly
  extracted from the daily-rollup pipeline.
- **Tariff-driven Cashflow chart.** Configurable feed-in tariff +
  import tariff + currency on `/settings` drive a Cashflow metric:
  `(export × feed_in) − (import × import_tariff)` per period. Distinct
  from SolisCloud's flat `production × tariff` Money figure.
- **Scheduler controls in Settings.** Run-poller toggle, sample
  cadence, daily-rollup time, API rate cap — all surfaced in the
  Settings UI; saving them takes effect in-process without a restart.
- **Resolution dropdown** (Auto / Samples / Daily / Monthly / Yearly)
  on the History page. Auto label reflects what it picked (e.g.
  *"Auto (monthly totals)"*).
- **Hover tooltips** on every metric tab (Pico's `data-tooltip`).
- **Favicon** — branded `static/favicon.png` (dark amber) served both
  via `<link rel="icon">` and `GET /favicon.ico`.

### Changed
- **Aligned auto-resolution thresholds.** Sample-only metrics
  (Power / Battery SOC / etc.) now bucket the same way as daily-rollup
  metrics for spans wider than 7 days — 8–31 days → daily, 32–732 →
  monthly, > 732 → yearly. Flipping tabs over the same range keeps the
  x-axis consistent.
- **Per-startup cache buster** on every vendored asset (`?v=…`) so a
  CSS / JS edit takes effect on the next page load without a manual
  hard refresh.
- **History page redesign.** Date-range From/To with quick presets
  replaces the old Range/single-date dropdown. Charts auto-refresh
  every 60s. Dense sample series render as a thin line (no area fill)
  so a day at 1-min cadence isn't a coloured block.
- **Tab strip rationalised** to 11 metrics: Energy, Power, Battery
  SOC, Battery power, Charge, Discharge, Consumption, Import, Export,
  Net, Cashflow. (Money / Total / Alarms-chart pruned — the dedicated
  /alarms nav page covers alarm browsing.)
- **Server-derived defaults** on the History page: if `station_daily`
  has rows, the page lands on Energy / Month; otherwise Power / Day.
- **Settings UI fully populated.** Tariffs, scheduler config, and the
  SolisCloud Key Secret (now visible as plain text — single-user
  dashboard behind login) are all editable from the page.
- **Purge scope narrowed.** Only drops `station_daily` and `stations`
  — the two collections every row of which can be re-downloaded from
  SolisCloud at any past date. `station_samples` (point-in-time polled
  data) and `alarms` (unclear upstream retention) are preserved.
- **Settings nav alignment.** Sign-out button now vertically centred
  in the top bar (was sitting visibly higher than the theme toggle
  because of an `inline-block` form wrapper).

### Fixed
- **HTMX `hx-disinherit` placement.** The backfill progress polling
  was firing once then dying because the polling fragment inherited
  the form's `hx-disabled-elt`, which raised `htmx:targetError` on
  every subsequent tick. `hx-disinherit` now sits on the form (its
  correct semantic location), so the progress bar updates every 700ms
  through completion.
- **Date-range "ignored" symptom.** Date inputs now refresh the chart
  on `change`, `blur`, and debounced `input` so typing a date doesn't
  require tabbing out before the chart catches up.
- **Tab-label invisibility on active tab.** Pico reassigns
  `--pico-color` to `#fff` inside `<button>` elements; using
  `var(--pico-color)` for the active-tab text rendered it white on
  white. Switched to `var(--pico-h1-color)`.
- **`get_solis_client` resilience** — no longer raises
  `AttributeError` when `app.state` is missing (the `ASGITransport`
  test path).
- **`poll_current` upserts.** `insert_one` was raising
  `DuplicateKeyError` when SolisCloud returned the same
  `dataTimestamp` on consecutive polls. Now upserts on
  `(station_id, ts)`.
- **Per-`station_samples` uniqueness.** Promoted the
  `(station_id, ts)` index to `unique`; backfill is now idempotent
  (deduped a stray batch from earlier polls during the migration).
- **Battery power tab 500.** `METRIC_BATTERY_POWER` was missing from
  `auto_range`'s sample-metric tuple and fell through to the daily
  branch. Pinned with a regression test.
- **Test isolation.** Conftest now sandboxes `XDG_CONFIG_HOME` to a
  per-session tmp dir so the reset-flow test no longer silently wipes
  the developer's real `~/.config/solisdash/solisdash.toml`. Forces
  `RUN_SCHEDULER=false` in the test process to keep `/ready` checks
  honest.

## 0.9.0 — 2026-05-17

### Fixed
- **The dashboard now actually downloads history data.** Previously,
  `RUN_SCHEDULER` defaulted to `False`, the wizard never turned it on,
  and the "Poll SolisCloud now" button only wrote one `station_samples`
  row — never `station_daily`. As a result the Month / Year / All-time
  charts stayed empty after a successful setup. Three changes fix this:
  1. The setup wizard now writes `RUN_SCHEDULER = true` to
     `solisdash.toml`, and the app starts its in-process scheduler
     immediately after wizard / Settings save without needing a
     process restart.
  2. The **Poll SolisCloud now** button now also calls `stationMonth`
     for every month from January 1 of the current year through today
     and upserts the rows into `station_daily`. The History page's
     Month / Year / All charts populate on the same click.
  3. A new `POST /history/backfill` endpoint and a "Fetch more history
     from SolisCloud" disclosure on the History page let you backfill
     an arbitrary date range (defaults to Jan 1 of last year → today)
     without needing the `invoke backfill` CLI.

### Added
- `_ensure_scheduler_running(app)` helper. Idempotent — safe to call
  from `lifespan`, the wizard, and the Settings-save path. The Settings
  page can now toggle background polling on or off and have the
  scheduler restart in-process.

## 0.8.2 — 2026-05-17

### Changed
- Housekeeping release. No user-visible changes. Backfilled GitHub
  Release pages for v0.7.0 – v0.8.1 with their full CHANGELOG entries
  so the project's release history is properly browseable from the
  Releases tab.

## 0.8.1 — 2026-05-16

### Fixed
- **HTMX buttons (Test MongoDB connection, Test SolisCloud, Poll
  SolisCloud now) silently did nothing inside pywebview.** HTMX was
  loaded from a CDN with SRI; pywebview's WKWebView dropped the script
  in some environments (offline, strict caching, SRI mismatches) and
  the buttons stopped working with no visible error. Solisdash now
  vendors HTMX, Pico CSS, Chart.js, and the Chart.js date-fns adapter
  under `static/vendor/`, so the entire UI works without network access
  to a CDN.

### Added
- `tests/test_clickability.py` regression suite. For every authed
  page (`/`, `/history`, `/alarms`, `/settings`) we extract every
  `<a href>`, `<form action>`, `[hx-{get,post,put,delete}]` URL
  pointing at our own app and assert each endpoint doesn't 5xx. The
  same file also asserts (a) no template references a CDN at runtime,
  (b) all four vendored assets are present on disk, and (c) `style.css`
  doesn't sneak in a `pointer-events: none` overlay.

## 0.8.0 — 2026-05-16

### Added
- **Metric tabs on the History page.** Power / Energy / Battery / Money /
  Alarms sit across the top; the range selector greys out options that
  aren't valid for the chosen metric (e.g. Money is daily-rollup only,
  so the Day option disables when Money is active). Each metric gets its
  own series colour. CSV download now follows the active metric.
- **24-hour sparklines under each live tile** on the home page. Tiny SVG
  trend lines drawn from `station_samples`, refreshed alongside the
  tiles via the existing 30-second HTMX swap. Server-rendered SVG paths,
  no extra JS or Chart.js needed.
- New JSON/CSV history endpoints accept `?metric=` (`power`, `energy`,
  `battery`, `money`, `alarms`). Existing callers without the parameter
  keep getting the previous default (power for day, energy elsewhere).

### Changed
- `HistoryService.day_series`, `month_daily`, `year_monthly`, `all_time`
  now take an optional `metric=` keyword. Defaults are unchanged.

### Fixed
- `LiveTilesService.get_tiles` falls through to the existing tile data
  if the sparkline lookup fails. Sparklines are decorative; a Mongo
  hiccup must never blank the home page.

## 0.7.3 — 2026-05-16

### Changed
- Softened the Settings page footnote about environment variables. The
  previous wording told every user that env vars "still take precedence
  over what's written here", which is jargon for a GUI-only client; now
  it just notes that same-named env vars may override saved values on
  managed deployments.

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
