# Solisdash

Self-hosted web dashboard for the SolisCloud (Ginlong Solis) platform.

Hybrid live tiles + historical charts. Shared SolisCloud key, simple multi-user login.

## Quick start

```
cp .env.example .env       # edit with your SolisCloud key + Atlas URI
uv sync
uv run python -m invoke start
```

App listens on http://127.0.0.1:8000. Health check at `/health`.

## Layout

- `src/solisdash/` — application package.
- `tests/` — pytest suite (parallel-safe under `pytest-xdist`).
- `tasks.py` — invoke build/admin tasks: `start`, `stop`, `restart`, `status`, `test`, `lint`.
- `tasks/todo.md` — current build plan.
- `CLAUDE.md` — project conventions for Claude Code sessions.
- `SolisCloud Platform API Document V2.0.3.pdf` — upstream API spec (source of truth).
