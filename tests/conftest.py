from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from solisdash.app import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
