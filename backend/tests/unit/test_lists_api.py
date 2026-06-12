"""The lists API: the owner toggling a list item from the PWA. Owner-only, with
a fake repo on app.state (the real RLS firewall is proven in test_lists_rls.py)."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.lists.service import ListItemInfo
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

NOW = datetime(2026, 6, 1, tzinfo=UTC)


class FakeLists:
    def __init__(self, stored: ListItemInfo | None) -> None:
        self.stored = stored
        self.calls: list[tuple[str, bool]] = []

    async def set_item_checked(self, ctx, item_id, *, checked):  # type: ignore[no-untyped-def]
        self.calls.append((item_id, checked))
        if self.stored is None or item_id != self.stored.id:
            return None
        return ListItemInfo(
            id=self.stored.id,
            body=self.stored.body,
            checked=checked,
            position=self.stored.position,
            source_note_id=None,
            created_at=NOW,
        )


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def lists() -> FakeLists:
    return FakeLists(ListItemInfo("i1", "eggs", False, 0, None, NOW))


@pytest.fixture
def client(repo: FakeAuthRepo, lists: FakeLists) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.lists_repo = lists
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_check_item_requires_owner(client: TestClient) -> None:
    assert client.patch("/api/lists/items/i1", json={"checked": True}).status_code == 401


def test_check_item_toggles_and_returns_it(
    client: TestClient, repo: FakeAuthRepo, lists: FakeLists
) -> None:
    login(client, repo)
    resp = client.patch("/api/lists/items/i1", json={"checked": True})
    assert resp.status_code == 200
    assert resp.json() == {"id": "i1", "body": "eggs", "checked": True}
    assert lists.calls == [("i1", True)]


def test_check_item_missing_is_404(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.patch("/api/lists/items/gone", json={"checked": True}).status_code == 404
