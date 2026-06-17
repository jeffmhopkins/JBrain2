"""Device-key HTTP Basic auth — service, header parsing, and the DI dependency.

Security path (L4): an unknown / revoked / wrong-kind key is fail-closed 401, and
an owner key presented on the device path never authenticates (kind-filtered).
"""

import asyncio
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api.deps import DeviceDep, _basic_password
from jbrain.auth import keys
from jbrain.auth import service as auth_service
from tests.unit.fakes import FakeAuthRepo


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _repo_with_device(key: str, *, kind: str = "device_key", revoked: bool = False) -> FakeAuthRepo:
    repo = FakeAuthRepo()
    asyncio.run(repo.create_principal(kind, keys.hash_key(key), "phone", subject_id="subject-1"))
    if revoked:
        for p in repo.principals:
            p.revoked = True
    return repo


# --- _basic_password ---------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (_basic("dev", "secret"), "secret"),
        (_basic("dev", "p:with:colons"), "p:with:colons"),  # only first ':' splits
        ("", None),
        ("Bearer abc", None),  # wrong scheme
        ("Basic", None),  # no payload
        ("Basic !!!notbase64!!!", None),  # undecodable
        ("Basic " + base64.b64encode(b"nocolon").decode(), None),  # no user:pass separator
    ],
)
def test_basic_password_parsing(header: str, expected: str | None) -> None:
    assert _basic_password(header) == expected


# --- authenticate_device -----------------------------------------------------


def test_authenticate_device_accepts_a_valid_device_key() -> None:
    repo = _repo_with_device("jb1-AAAA-BBBB")
    principal = asyncio.run(auth_service.authenticate_device(repo, "jb1-AAAA-BBBB"))
    assert principal is not None
    assert principal.kind == "device_key"
    assert principal.subject_id == "subject-1"


def test_authenticate_device_rejects_unknown_revoked_empty_and_owner_keys() -> None:
    repo = _repo_with_device("jb1-AAAA-BBBB")
    assert asyncio.run(auth_service.authenticate_device(repo, "jb1-WRONG")) is None
    assert asyncio.run(auth_service.authenticate_device(repo, "")) is None
    revoked = _repo_with_device("jb1-CCCC", revoked=True)
    assert asyncio.run(auth_service.authenticate_device(revoked, "jb1-CCCC")) is None
    # An owner key on the device path is kind-filtered out (no confusion).
    owner = _repo_with_device("jb1-DDDD", kind="owner")
    assert asyncio.run(auth_service.authenticate_device(owner, "jb1-DDDD")) is None


# --- current_device_principal (the DI dependency) ----------------------------


def _probe_app(repo: FakeAuthRepo) -> TestClient:
    app = FastAPI()

    @app.get("/_probe")
    async def probe(p: DeviceDep) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"subject_id": p.subject_id, "kind": p.kind}

    app.state.auth_repo = repo
    return TestClient(app)


def test_device_dependency_authorizes_then_401s() -> None:
    c = _probe_app(_repo_with_device("jb1-KEY"))
    ok = c.get("/_probe", headers={"Authorization": _basic("phone", "jb1-KEY")})
    assert ok.status_code == 200
    assert ok.json() == {"subject_id": "subject-1", "kind": "device_key"}

    # No header, wrong key, and an owner key all fail closed.
    assert c.get("/_probe").status_code == 401
    assert c.get("/_probe", headers={"Authorization": _basic("phone", "nope")}).status_code == 401
    owner = _probe_app(_repo_with_device("jb1-OWN", kind="owner"))
    assert (
        owner.get("/_probe", headers={"Authorization": _basic("o", "jb1-OWN")}).status_code == 401
    )
