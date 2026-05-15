from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

import solisdash


def test_package_version() -> None:
    assert solisdash.__version__


def test_version_matches_pyproject() -> None:
    """`__version__` and `pyproject.toml [project].version` are two hand-kept
    sources of truth — drift here means a release bumps one but not the other.
    """
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert solisdash.__version__ == data["project"]["version"]


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == solisdash.__version__
