"""The internal MQTT auth/ACL endpoints (go-auth HTTP backend) with a fake repo.

Security path (T2): only an active `device_key` whose claimed username equals its
own principal id authenticates; owner/capability keys and forged identities are
fail-closed. The ACL then confines a device to its own namespace.
"""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import keys
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

KEY = "jb1-AAAA-BBBB"


def _make(*, kind: str = "device_key", revoked: bool = False) -> tuple[FakeAuthRepo, str]:
    repo = FakeAuthRepo()
    asyncio.run(repo.create_principal(kind, keys.hash_key(KEY), "phone", subject_id="subject-1"))
    if revoked:
        repo.principals[0].revoked = True
    return repo, repo.principals[0].id


@pytest.fixture
def client() -> Iterator[tuple[TestClient, str]]:
    """An app whose auth_repo holds one active device; yields (client, principal_id)."""
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    repo, pid = _make()
    with TestClient(app) as c:
        app.state.auth_repo = repo
        yield c, pid


# --- /internal/mqtt-auth -----------------------------------------------------


def test_auth_accepts_active_device_bound_to_its_own_username(
    client: tuple[TestClient, str],
) -> None:
    c, pid = client
    r = c.post("/internal/mqtt-auth", json={"username": pid, "password": KEY, "clientid": pid})
    assert r.status_code == 200


def test_auth_rejects_wrong_key_and_missing_password(client: tuple[TestClient, str]) -> None:
    c, pid = client
    assert (
        c.post("/internal/mqtt-auth", json={"username": pid, "password": "nope"}).status_code == 403
    )
    assert c.post("/internal/mqtt-auth", json={"username": pid, "password": ""}).status_code == 403


def test_auth_rejects_forged_username_even_with_a_valid_key(
    client: tuple[TestClient, str],
) -> None:
    c, _ = client
    # Valid key, but claiming a different identity than the key's principal.
    r = c.post("/internal/mqtt-auth", json={"username": "someone-else", "password": KEY})
    assert r.status_code == 403


def test_auth_rejects_revoked_and_wrong_kind_keys() -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    for kind, revoked in [("device_key", True), ("owner", False), ("capability_token", False)]:
        app = create_app(settings)
        repo, pid = _make(kind=kind, revoked=revoked)
        with TestClient(app) as c:
            app.state.auth_repo = repo
            r = c.post("/internal/mqtt-auth", json={"username": pid, "password": KEY})
            assert r.status_code == 403, (kind, revoked)


# --- /internal/mqtt-acl ------------------------------------------------------


def test_acl_allows_only_the_devices_own_namespace(client: tuple[TestClient, str]) -> None:
    c, pid = client

    def acl(topic: str, acc: int) -> int:
        return c.post(
            "/internal/mqtt-acl",
            json={"username": pid, "clientid": pid, "topic": topic, "acc": acc},
        ).status_code

    assert acl(f"owntracks/{pid}/phone", 2) == 200  # publish own location
    assert acl(f"owntracks/{pid}/phone/cmd", 1) == 200  # subscribe own cmd
    assert acl("owntracks/someone-else/phone", 1) == 403  # foreign subject
    assert acl("owntracks/+/+", 1) == 403  # broad wildcard
    assert acl("#", 1) == 403
