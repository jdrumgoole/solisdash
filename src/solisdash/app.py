from __future__ import annotations

from fastapi import FastAPI

from solisdash import __version__

app = FastAPI(title="Solisdash", version=__version__)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
