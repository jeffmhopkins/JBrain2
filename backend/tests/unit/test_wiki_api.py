"""The /api/wiki/landing + /api/wiki/{id} surface, with a stubbed read store: auth required,
the static `landing` route wins over `{id}`, and a missing article is a 404."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

_ARTICLE = {
    "id": "a1",
    "title": "Priya Nair",
    "subtitle": "Person · machine-written from your notes",
    "infobox": {"title": "Priya Nair", "kind": "Person", "photo": False, "fields": []},
    "lead": [{"kind": "p", "text": "Priya is a pediatrician.[1]"}],
    "sections": [],
    "references": [],
}


class StubWikiReadStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_landing(self, ctx: Any) -> dict[str, Any]:
        self.calls.append(("landing", ""))
        return {"recent": [], "hubs": [], "groups": []}

    async def get_article(self, ctx: Any, article_id: str) -> dict[str, Any] | None:
        self.calls.append(("article", article_id))
        return _ARTICLE if article_id == "a1" else None


@pytest.fixture
def api() -> Iterator[tuple[TestClient, StubWikiReadStore]]:
    stub = StubWikiReadStore()
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        app.state.wiki_read_store = stub
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, stub


def test_wiki_requires_auth() -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/wiki/landing").status_code == 401
        assert anon.get("/api/wiki/a1").status_code == 401


def test_landing_route_is_not_swallowed_by_the_id_route(
    api: tuple[TestClient, StubWikiReadStore],
) -> None:
    client, stub = api
    body = client.get("/api/wiki/landing").json()
    assert body == {"recent": [], "hubs": [], "groups": []}
    assert stub.calls[-1] == ("landing", "")  # hit get_landing, NOT get_article("landing")


def test_article_returns_the_reader_shape(api: tuple[TestClient, StubWikiReadStore]) -> None:
    client, _ = api
    body = client.get("/api/wiki/a1").json()
    assert body["title"] == "Priya Nair"
    assert body["infobox"]["kind"] == "Person"
    assert body["lead"][0]["text"].endswith("[1]")


def test_missing_article_is_404(api: tuple[TestClient, StubWikiReadStore]) -> None:
    client, _ = api
    assert client.get("/api/wiki/nope").status_code == 404
