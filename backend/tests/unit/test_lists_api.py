"""The lists API: the owner managing lists from the PWA. Owner-only, with a fake
repo on app.state (the real RLS firewall is proven in test_lists_rls.py); these
assert each endpoint wires to the right repo call and maps misses to 404."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.lists.service import ListInfo, ListItemInfo, UnknownDomain
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

NOW = datetime(2026, 6, 1, tzinfo=UTC)
ITEM = ListItemInfo("i1", "eggs", False, 0, None, NOW)
LIST = ListInfo("L1", "general", "Groceries", False, NOW, NOW, [ITEM])


class FakeLists:
    """Records calls and returns canned rows; `ok=False` makes the lookups miss."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.ok = True
        self.bad_domain = False

    async def list_lists(self, ctx, *, include_archived=False):  # type: ignore[no-untyped-def]
        self.calls.append(("list_lists", include_archived))
        return [LIST]

    async def create_list(self, ctx, *, domain, title):  # type: ignore[no-untyped-def]
        self.calls.append(("create_list", domain, title))
        if self.bad_domain:
            raise UnknownDomain(domain)
        return ListInfo("new", domain, title, False, NOW, NOW, [])

    async def rename_list(self, ctx, list_id, title):  # type: ignore[no-untyped-def]
        self.calls.append(("rename_list", list_id, title))
        return LIST if self.ok else None

    async def archive_list(self, ctx, list_id, *, archived):  # type: ignore[no-untyped-def]
        self.calls.append(("archive_list", list_id, archived))
        return LIST if self.ok else None

    async def delete_list(self, ctx, list_id):  # type: ignore[no-untyped-def]
        self.calls.append(("delete_list", list_id))
        return self.ok

    async def add_item(self, ctx, list_id, body, *, source_note_id=None):  # type: ignore[no-untyped-def]
        self.calls.append(("add_item", list_id, body))
        return ITEM if self.ok else None

    async def reorder_items(self, ctx, list_id, item_ids):  # type: ignore[no-untyped-def]
        self.calls.append(("reorder", list_id, item_ids))
        return self.ok

    async def set_item_checked(self, ctx, item_id, *, checked):  # type: ignore[no-untyped-def]
        self.calls.append(("check", item_id, checked))
        return ListItemInfo(item_id, "eggs", checked, 0, None, NOW) if self.ok else None

    async def rename_item(self, ctx, item_id, body):  # type: ignore[no-untyped-def]
        self.calls.append(("rename_item", item_id, body))
        return ListItemInfo(item_id, body, False, 0, None, NOW) if self.ok else None

    async def remove_item(self, ctx, item_id):  # type: ignore[no-untyped-def]
        self.calls.append(("remove_item", item_id))
        return self.ok


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def lists() -> FakeLists:
    return FakeLists()


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


def test_lists_require_owner(client: TestClient) -> None:
    assert client.get("/api/lists").status_code == 401


def test_list_lists_returns_lists_with_items(
    client: TestClient, repo: FakeAuthRepo, lists: FakeLists
) -> None:
    login(client, repo)
    data = client.get("/api/lists").json()
    assert data == [
        {
            "id": "L1",
            "title": "Groceries",
            "domain": "general",
            "archived": False,
            "items": [{"id": "i1", "body": "eggs", "checked": False}],
        }
    ]


def test_create_list_and_unknown_domain(
    client: TestClient, repo: FakeAuthRepo, lists: FakeLists
) -> None:
    login(client, repo)
    resp = client.post("/api/lists", json={"title": "Packing", "domain": "general"})
    assert resp.status_code == 201 and resp.json()["title"] == "Packing"
    assert lists.calls[-1] == ("create_list", "general", "Packing")
    lists.bad_domain = True
    assert client.post("/api/lists", json={"title": "x", "domain": "bogus"}).status_code == 422


def test_patch_list_rename_archive_and_miss(
    client: TestClient, repo: FakeAuthRepo, lists: FakeLists
) -> None:
    login(client, repo)
    assert client.patch("/api/lists/L1", json={"title": "Food"}).status_code == 200
    assert lists.calls[-1] == ("rename_list", "L1", "Food")
    assert client.patch("/api/lists/L1", json={"archived": True}).status_code == 200
    assert lists.calls[-1] == ("archive_list", "L1", True)
    lists.ok = False
    assert client.patch("/api/lists/gone", json={"title": "x"}).status_code == 404


def test_delete_list(client: TestClient, repo: FakeAuthRepo, lists: FakeLists) -> None:
    login(client, repo)
    assert client.delete("/api/lists/L1").status_code == 204
    assert lists.calls[-1] == ("delete_list", "L1")
    lists.ok = False
    assert client.delete("/api/lists/gone").status_code == 404


def test_add_item(client: TestClient, repo: FakeAuthRepo, lists: FakeLists) -> None:
    login(client, repo)
    resp = client.post("/api/lists/L1/items", json={"body": "milk"})
    assert resp.status_code == 201
    assert lists.calls[-1] == ("add_item", "L1", "milk")
    lists.ok = False
    assert client.post("/api/lists/gone/items", json={"body": "x"}).status_code == 404


def test_reorder(client: TestClient, repo: FakeAuthRepo, lists: FakeLists) -> None:
    login(client, repo)
    assert client.patch("/api/lists/L1/order", json={"item_ids": ["b", "a"]}).status_code == 204
    assert lists.calls[-1] == ("reorder", "L1", ["b", "a"])
    lists.ok = False
    assert client.patch("/api/lists/gone/order", json={"item_ids": []}).status_code == 404


def test_patch_item_check_and_rename(
    client: TestClient, repo: FakeAuthRepo, lists: FakeLists
) -> None:
    login(client, repo)
    checked = client.patch("/api/lists/items/i1", json={"checked": True})
    assert checked.status_code == 200 and checked.json()["checked"] is True
    assert lists.calls[-1] == ("check", "i1", True)
    renamed = client.patch("/api/lists/items/i1", json={"body": "free-range eggs"})
    assert renamed.status_code == 200 and renamed.json()["body"] == "free-range eggs"
    assert lists.calls[-1] == ("rename_item", "i1", "free-range eggs")
    lists.ok = False
    assert client.patch("/api/lists/items/gone", json={"checked": True}).status_code == 404


def test_remove_item(client: TestClient, repo: FakeAuthRepo, lists: FakeLists) -> None:
    login(client, repo)
    assert client.delete("/api/lists/items/i1").status_code == 204
    assert lists.calls[-1] == ("remove_item", "i1")
    lists.ok = False
    assert client.delete("/api/lists/items/gone").status_code == 404
