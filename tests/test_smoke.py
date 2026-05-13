from __future__ import annotations

from fastapi.testclient import TestClient

import solisdash


def test_package_version() -> None:
    assert solisdash.__version__


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == solisdash.__version__
