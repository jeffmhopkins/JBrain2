"""Owner-only family-membership API with a fake repo. The RLS firewall itself
(owner-only writes, add/remove toggling family-sees-family) is proven against real
Postgres in tests/integration/test_family_admin_pg.py."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.family import FamilyMember
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


class FakeFamilyRepo:
    def __init__(self) -> None:
        self.members_list = [
            FamilyMember(
                subject_id="sub-1", label="Alice", added_at=datetime(2026, 6, 1, tzinfo=UTC)
            )
        ]
        self.added: list[str] = []
        self.removed: list[str] = []

    async def members(self, ctx):  # noqa: ANN001
        return self.members_list

    async def add_member(self, ctx, subject_id):  # noqa: ANN001
        self.added.append(subject_id)

    async def remove_member(self, ctx, subject_id):  # noqa: ANN001
        self.removed.append(subject_id)


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeFamilyRepo]]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    repo = FakeFamilyRepo()
    auth_repo = FakeAuthRepo()
    with TestClient(app) as c:
        app.state.auth_repo = auth_repo
        app.state.family_repo = repo
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            c.post("/api/auth/session", json={"owner_key": key, "device_label": "t"}).status_code
            == 204
        )
        yield c, repo


def test_lists_members(client: tuple[TestClient, FakeFamilyRepo]) -> None:
    c, _ = client
    data = c.get("/api/family/members").json()
    assert [m["subject_id"] for m in data] == ["sub-1"]
    assert data[0]["label"] == "Alice"


def test_add_and_remove_member(client: tuple[TestClient, FakeFamilyRepo]) -> None:
    c, repo = client
    assert c.post("/api/family/members", json={"subject_id": "sub-2"}).status_code == 204
    assert repo.added == ["sub-2"]
    assert c.delete("/api/family/members/sub-2").status_code == 204
    assert repo.removed == ["sub-2"]


def test_family_routes_are_owner_only() -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        app.state.family_repo = FakeFamilyRepo()
        # No session at all -> 401.
        assert anon.get("/api/family/members").status_code == 401
        assert anon.post("/api/family/members", json={"subject_id": "x"}).status_code == 401
        assert anon.delete("/api/family/members/x").status_code == 401
