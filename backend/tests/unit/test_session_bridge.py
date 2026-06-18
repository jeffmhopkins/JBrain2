"""The dashboard session bridge (JBrain360 M4a): /session/mint exchanges the
device key for the member session cookie. Fakes on app.state; the SQL device
lookup is proven against real Postgres elsewhere. These assert the cookie is
minted only for a live device key, that owner/capability/revoked keys are
rejected, and that the minted session is a *member* session — barred from owner
routes (it unlocks the member reads in M4b, not the owner surface)."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import keys, service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo, FakePrincipal

DEVICE_KEY = "jb-device-secret-key"
SUBJECT = "subject-kid-1"


@pytest.fixture
def repo() -> FakeAuthRepo:
    r = FakeAuthRepo()
    r.principals.append(
        FakePrincipal(
            id="dev-1",
            kind="device_key",
            key_hash=keys.hash_key(DEVICE_KEY),
            label="Kid Phone",
            subject_id=SUBJECT,
        )
    )
    return r


@pytest.fixture
def client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        yield test_client


def _mint_cookie(resp) -> str | None:  # noqa: ANN001 - httpx Response
    return resp.cookies.get("jbrain_session")


def test_mint_sets_member_cookie_bound_to_subject(client: TestClient) -> None:
    resp = client.post("/api/session/mint", json={"device_key": DEVICE_KEY})
    assert resp.status_code == 204
    assert _mint_cookie(resp) is not None
    # The cookie authenticates as the *device* principal, subject pinned.
    me = client.get("/api/auth/me").json()
    assert me["kind"] == "device_key"
    assert me["principal_id"] == "dev-1"


def test_mint_cookie_attributes_are_locked_down(client: TestClient) -> None:
    resp = client.post("/api/session/mint", json={"device_key": DEVICE_KEY})
    set_cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def test_mint_rejects_unknown_key(client: TestClient) -> None:
    resp = client.post("/api/session/mint", json={"device_key": "not-a-real-key"})
    assert resp.status_code == 401
    assert _mint_cookie(resp) is None


def test_mint_rejects_owner_key(client: TestClient, repo: FakeAuthRepo) -> None:
    # An owner key must never mint a device dashboard session (kind-filtered lookup).
    owner_key = asyncio.run(service.rotate_owner_key(repo))
    resp = client.post("/api/session/mint", json={"device_key": owner_key})
    assert resp.status_code == 401
    assert _mint_cookie(resp) is None


def test_mint_rejects_revoked_key(client: TestClient, repo: FakeAuthRepo) -> None:
    repo.principals[0].revoked = True
    resp = client.post("/api/session/mint", json={"device_key": DEVICE_KEY})
    assert resp.status_code == 401


def test_member_session_cannot_reach_owner_routes(client: TestClient) -> None:
    assert client.post("/api/session/mint", json={"device_key": DEVICE_KEY}).status_code == 204
    # The member cookie is set on the client; owner-only routes still 403 it.
    assert client.get("/api/locations/devices").status_code == 403
    assert client.get("/api/sessions").status_code == 403
