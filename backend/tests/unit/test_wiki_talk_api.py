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
from jbrain.wiki.editor import EditorReply
from jbrain.wiki.talkstore import (
    TalkArticleNotFound,
    TalkBuildLogReadonly,
    TalkEditorConflict,
    TalkTopicNotFound,
)
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

    async def topic_for_editor(
        self, ctx: Any, article_id: str, topic_id: str, after_post_id: str
    ) -> Any:
        if self.raise_with:
            raise self.raise_with
        return ("Outdated", "Celine", [{"author": "owner", "body": "fix it"}])

    async def add_editor_post(
        self, ctx: Any, article_id: str, topic_id: str, *, body: str, outcome: str | None
    ) -> Any:
        return {"id": "ed1", "author": "editor", "body": body, "outcome": outcome}


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
        # The editor endpoint reads these and hands them to run_editor_turn (stubbed in its tests).
        app.state.llm_router = object()
        app.state.agent_registry = object()
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


def _patch_editor(monkeypatch: pytest.MonkeyPatch, reply: EditorReply | None) -> None:
    async def _fake(*_args: Any, **_kwargs: Any) -> EditorReply | None:
        return reply

    monkeypatch.setattr("jbrain.api.wiki.run_editor_turn", _fake)


def _editor(client: TestClient, topic: str, after: str) -> Any:
    return client.post(f"/api/wiki/a1/talk/topics/{topic}/editor", json={"after_post_id": after})


def test_editor_posts_the_reply_with_chip(
    api: tuple[TestClient, StubTalkStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    _patch_editor(
        monkeypatch, EditorReply("Here's the sourcing.", "correction filed → rebuild queued")
    )  # noqa: E501
    out = _editor(client, "t1", "p9")
    assert out.status_code == 201
    post = out.json()["post"]
    assert post["author"] == "editor" and post["outcome"].startswith("correction filed")


def test_editor_null_when_turn_yields_nothing(
    api: tuple[TestClient, StubTalkStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    _patch_editor(monkeypatch, None)
    out = _editor(client, "t1", "p9")
    assert out.status_code == 201 and out.json()["post"] is None


def test_editor_conflict_and_guards(
    api: tuple[TestClient, StubTalkStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, store = api
    _patch_editor(monkeypatch, EditorReply("x", None))  # never reached — store guards fire first
    store.raise_with = TalkEditorConflict()
    assert _editor(client, "t1", "old").status_code == 409
    store.raise_with = TalkBuildLogReadonly()
    assert _editor(client, "log", "p").status_code == 409
    store.raise_with = TalkTopicNotFound()
    assert _editor(client, "x", "p").status_code == 404


def test_editor_requires_after_post_id(api: tuple[TestClient, StubTalkStore]) -> None:
    client, _ = api
    assert client.post("/api/wiki/a1/talk/topics/t1/editor", json={}).status_code == 422


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
        editor = client.post("/api/wiki/a1/talk/topics/t1/editor", json={"after_post_id": "p"})
        assert new.status_code == 403
        assert reply.status_code == 403
        assert patched.status_code == 403
        assert editor.status_code == 403
    finally:
        app.dependency_overrides.pop(current_principal, None)
