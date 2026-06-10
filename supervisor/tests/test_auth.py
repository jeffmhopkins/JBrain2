"""Security path: every authenticated route rejects bad or missing tokens."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH, TOKEN

PROTECTED_GETS = ["/status", "/logs/api", "/logs/api/stream"]


def test_healthz_is_unauthenticated(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_get_without_token_is_401(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_restart_without_token_is_401(client: TestClient) -> None:
    assert client.post("/restart", json={"service": "api"}).status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {TOKEN}x",
        f"Bearer {TOKEN[:-1]}",
        "Bearer ",
        f"Basic {TOKEN}",
        TOKEN,
    ],
)
def test_wrong_credentials_are_401(client: TestClient, header: str) -> None:
    response = client.get("/status", headers={"Authorization": header})
    assert response.status_code == 401


def test_correct_token_is_accepted(client: TestClient) -> None:
    assert client.get("/status", headers=AUTH).status_code == 200
