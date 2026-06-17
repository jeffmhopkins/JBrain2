"""The Talk-board API surface (GET board + owner-gated new-topic/reply/resolve) with a stubbed
store: auth required, owner-only writes, 409 on the Build-log, 404s, and min_length validation."""

import asyncio
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.wiki.talkstore import TalkArticleNotFound, TalkBuildLogReadonly, TalkTopicNotFound
from tests.unit.fakes import FakeAuthRepo

BOARD = {"title": "Celine", "topics": [{"id": "t1", "kind": "build_log", "posts": []}]}


class StubTalkStore:
    def __init__(self) -> None:
        self.raise_with: Exception | None = None

    async def get_board(self, ctx: Any, article_id: str) -> dict[str, Any] | None:
        return BOARD if article_id == "a1" else None

    async def create_topic(self, ctx: Any, article_id: str, *, title: str, body: str) -> Any:
        if self.raise_with:
            raise self.raise_with
        return {"id": "new", "kind": "discussion", "title": title, "status": "open", "posts": []}

    async def add_reply(self, ctx: Any, article_id: str, topic_id: str, *, body: str) -> Any:
        if self.raise_with:
            raise self.raise_with
        return {"id": "p2", "author": "owner", "body": body}

    async def set_status(self, ctx: Any, article_id: str, topic_id: str, *, status: str) -> Any:
        if self.raise_with:
            raise self.raise_with
        return {"id": topic_id, "status": status}


@pytest.fixture
def api() -> Iterator[tuple[TestClient, StubTalkStore]]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    store = StubTalkStore()
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        app.state.wiki_talk_store = store
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, store


def test_talk_requires_auth() -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/wiki/a1/talk").status_code == 401
        new = anon.post("/api/wiki/a1/talk/topics", json={"title": "x", "body": "y"})
        assert new.status_code == 401


def test_get_board(api: tuple[TestClient, StubTalkStore]) -> None:
    client, _ = api
    assert client.get("/api/wiki/a1/talk").json()["title"] == "Celine"
    assert client.get("/api/wiki/nope/talk").status_code == 404


def test_new_topic_and_reply_and_resolve(api: tuple[TestClient, StubTalkStore]) -> None:
    client, _ = api
    created = client.post("/api/wiki/a1/talk/topics", json={"title": "Outdated", "body": "fix it"})
    assert created.status_code == 201 and created.json()["kind"] == "discussion"
    reply = client.post("/api/wiki/a1/talk/topics/t1/posts", json={"body": "please"})
    assert reply.status_code == 201 and reply.json()["author"] == "owner"
    patched = client.patch("/api/wiki/a1/talk/topics/t1", json={"status": "resolved"})
    assert patched.status_code == 200 and patched.json()["status"] == "resolved"


def test_validation_rejects_empty_title_and_body(api: tuple[TestClient, StubTalkStore]) -> None:
    client, _ = api
    bad_topic = client.post("/api/wiki/a1/talk/topics", json={"title": "", "body": "y"})
    bad_reply = client.post("/api/wiki/a1/talk/topics/t1/posts", json={"body": ""})
    bad_status = client.patch("/api/wiki/a1/talk/topics/t1", json={"status": "bogus"})
    assert bad_topic.status_code == 422
    assert bad_reply.status_code == 422
    assert bad_status.status_code == 422


def test_build_log_post_is_409(api: tuple[TestClient, StubTalkStore]) -> None:
    client, store = api
    store.raise_with = TalkBuildLogReadonly()
    assert client.post("/api/wiki/a1/talk/topics/log/posts", json={"body": "x"}).status_code == 409
    assert client.patch("/api/wiki/a1/talk/topics/log", json={"status": "open"}).status_code == 409


def test_missing_article_or_topic_is_404(api: tuple[TestClient, StubTalkStore]) -> None:
    client, store = api
    store.raise_with = TalkArticleNotFound()
    missing_article = client.post("/api/wiki/x/talk/topics", json={"title": "a", "body": "b"})
    assert missing_article.status_code == 404
    store.raise_with = TalkTopicNotFound()
    missing_topic = client.post("/api/wiki/a1/talk/topics/x/posts", json={"body": "b"})
    assert missing_topic.status_code == 404


def test_writes_are_owner_only(api: tuple[TestClient, StubTalkStore]) -> None:
    client, _ = api
    app = cast(FastAPI, client.app)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="cap-1", kind="capability_token", label="agent"
    )
    try:
        new = client.post("/api/wiki/a1/talk/topics", json={"title": "a", "body": "b"})
        reply = client.post("/api/wiki/a1/talk/topics/t1/posts", json={"body": "b"})
        patched = client.patch("/api/wiki/a1/talk/topics/t1", json={"status": "open"})
        assert new.status_code == 403
        assert reply.status_code == 403
        assert patched.status_code == 403
    finally:
        app.dependency_overrides.pop(current_principal, None)
