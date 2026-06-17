"""Hybrid-search fusion logic and the /api/search surface, all with fakes."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.main import create_app
from jbrain.search.service import (
    RRF_K,
    ChunkHit,
    SearchResponse,
    SearchResult,
    SearchService,
    WikiHit,
    WikiSearchResult,
    rrf_scores,
    truncate,
)
from tests.unit.fakes import FakeAuthRepo


def notes_only(resp: SearchResponse) -> list[SearchResult]:
    """The note hits from a (note|wiki) result list — narrows the union for note assertions."""
    return [r for r in resp.results if isinstance(r, SearchResult)]


NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def hit(chunk_id: str, note_id: str = "n1", text: str = "some text", **kw) -> ChunkHit:
    defaults = dict(
        source_kind="note",
        source_anchor=None,
        domain="general",
        destination=None,
        created_at=NOW,
        body="note body",
        attachment_count=0,
        headline=None,
    )
    return ChunkHit(chunk_id=chunk_id, note_id=note_id, text=text, **{**defaults, **kw})


def whit(section_id: str, article_id: str = "a1", **kw) -> WikiHit:
    defaults = dict(
        title="An Article",
        blurb="a blurb",
        entity_kind="Person",
        domain="general",
        text="wiki body text",
        headline=None,
    )
    return WikiHit(section_id=section_id, article_id=article_id, **{**defaults, **kw})


class FakeEmbed:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        if self.fail:
            raise ConnectionError("embed container down")
        return [[1.0, 0.0, 0.0] for _ in texts]


@dataclass
class FakeSearchRepo:
    dense: list[ChunkHit] = field(default_factory=list)
    fts: list[ChunkHit] = field(default_factory=list)
    wiki_dense: list[WikiHit] = field(default_factory=list)
    wiki_fts: list[WikiHit] = field(default_factory=list)
    dense_calls: int = 0
    wiki_dense_calls: int = 0

    async def dense_search(
        self, ctx: SessionContext, qvec: list[float], domain: str | None, limit: int
    ) -> list[ChunkHit]:
        self.dense_calls += 1
        return self.dense[:limit]

    async def fts_search(
        self, ctx: SessionContext, q: str, domain: str | None, limit: int
    ) -> list[ChunkHit]:
        return self.fts[:limit]

    async def wiki_dense_search(
        self, ctx: SessionContext, qvec: list[float], domain: str | None, limit: int
    ) -> list[WikiHit]:
        self.wiki_dense_calls += 1
        return self.wiki_dense[:limit]

    async def wiki_fts_search(
        self, ctx: SessionContext, q: str, domain: str | None, limit: int
    ) -> list[WikiHit]:
        return self.wiki_fts[:limit]


CTX = SessionContext(principal_id="p", principal_kind="owner")


def test_rrf_rewards_presence_in_both_rankings() -> None:
    scores = rrf_scores(["a", "b"], ["b", "c"])
    assert scores["b"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert scores["b"] > scores["a"] > scores["c"]


def test_truncate_appends_ellipsis_only_when_needed() -> None:
    assert truncate("short", 10) == "short"
    assert truncate("a" * 12, 10) == "a" * 10 + "…"


async def test_both_legs_fuse_with_match_labels() -> None:
    repo = FakeSearchRepo(
        dense=[hit("c-both", note_id="n1"), hit("c-dense", note_id="n2")],
        fts=[hit("c-both", note_id="n1", headline="<mark>x</mark>"), hit("c-fts", note_id="n3")],
    )
    resp = await SearchService(repo, FakeEmbed()).search(CTX, "x", None, 20)
    assert not resp.degraded
    notes = notes_only(resp)
    by_chunk = {r.chunk_id: r for r in notes}
    assert by_chunk["c-both"].match == "both"
    assert by_chunk["c-dense"].match == "semantic"
    assert by_chunk["c-fts"].match == "keyword"
    # The chunk in both legs outranks every single-leg chunk.
    assert notes[0].chunk_id == "c-both"


async def test_degraded_when_embedding_fails() -> None:
    repo = FakeSearchRepo(fts=[hit("c1", headline="<mark>hi</mark>")])
    resp = await SearchService(repo, FakeEmbed(fail=True)).search(CTX, "hi", None, 20)
    assert resp.degraded
    assert repo.dense_calls == 0  # dense leg skipped, never errored
    assert [r.match for r in resp.results] == ["keyword"]
    assert resp.results[0].snippet == "<mark>hi</mark>"


async def test_snippets_prefer_headline_else_truncated_text() -> None:
    long_text = "word " * 100
    repo = FakeSearchRepo(
        dense=[hit("c-dense", note_id="n2", text=long_text)],
        fts=[hit("c-fts", note_id="n1", headline="<mark>found</mark> it")],
    )
    resp = await SearchService(repo, FakeEmbed()).search(CTX, "q", None, 20)
    by_chunk = {r.chunk_id: r for r in notes_only(resp)}
    assert by_chunk["c-fts"].snippet == "<mark>found</mark> it"
    assert len(by_chunk["c-dense"].snippet) <= 241  # 240 + ellipsis
    assert by_chunk["c-dense"].snippet.endswith("…")


async def test_results_group_to_best_chunk_per_note() -> None:
    # n1 appears via two chunks; only its best (the fused one) survives.
    repo = FakeSearchRepo(
        dense=[hit("c1", note_id="n1"), hit("c2", note_id="n1")],
        fts=[hit("c1", note_id="n1")],
    )
    resp = await SearchService(repo, FakeEmbed()).search(CTX, "q", None, 20)
    assert [r.chunk_id for r in notes_only(resp)] == ["c1"]


async def test_limit_and_preview() -> None:
    repo = FakeSearchRepo(dense=[hit(f"c{i}", note_id=f"n{i}", body="b" * 500) for i in range(5)])
    resp = await SearchService(repo, FakeEmbed()).search(CTX, "q", None, 2)
    notes = notes_only(resp)
    assert len(notes) == 2
    assert all(len(r.body_preview) == 161 for r in notes)  # 160 + ellipsis
    assert all(r.body_preview.endswith("…") for r in notes)


class StubSearchService:
    def __init__(self, response):
        self._response = response
        self.calls: list[tuple[str, str | None, int]] = []

    async def search(self, ctx, q, domain, limit):
        self.calls.append((q, domain, limit))
        return self._response


@pytest.fixture
def api() -> Iterator[tuple[TestClient, StubSearchService]]:
    from jbrain.search.service import SearchResponse, SearchResult

    stub = StubSearchService(
        SearchResponse(
            degraded=True,
            results=[
                SearchResult(
                    note_id="n1",
                    chunk_id="c1",
                    snippet="<mark>hello</mark>",
                    match="keyword",
                    score=0.016,
                    domain="general",
                    destination="Inbox",
                    created_at=NOW,
                    body_preview="hello world",
                    attachment_count=2,
                    source_kind="note",
                    source_anchor=None,
                )
            ],
        )
    )
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    auth_repo = FakeAuthRepo()
    with TestClient(app) as client:
        app.state.auth_repo = auth_repo
        app.state.search_service = stub
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, stub


def test_search_requires_auth() -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/search", params={"q": "x"}).status_code == 401


def test_search_response_shape(api: tuple[TestClient, StubSearchService]) -> None:
    client, _ = api
    body = client.get("/api/search", params={"q": "hello"}).json()
    assert body["degraded"] is True
    assert body["results"] == [
        {
            "kind": "note",
            "note_id": "n1",
            "chunk_id": "c1",
            "snippet": "<mark>hello</mark>",
            "match": "keyword",
            "score": 0.016,
            "domain": "general",
            "destination": "Inbox",
            "created_at": "2026-06-10T12:00:00Z",
            "body_preview": "hello world",
            "attachment_count": 2,
            "source_kind": "note",
            "source_anchor": None,
        }
    ]


async def test_wiki_leg_fuses_and_heads_the_results() -> None:
    # A wiki article matching both legs heads the list, above note hits, carrying kind='wiki'.
    repo = FakeSearchRepo(
        dense=[hit("c-dense", note_id="n1")],
        fts=[hit("c-fts", note_id="n2")],
        wiki_dense=[whit("s1", article_id="a1", title="Priya Nair")],
        wiki_fts=[whit("s1", article_id="a1", title="Priya Nair", headline="<mark>Priya</mark>")],
    )
    resp = await SearchService(repo, FakeEmbed()).search(CTX, "priya", None, 20)
    assert repo.wiki_dense_calls == 1
    first = resp.results[0]
    assert isinstance(first, WikiSearchResult)
    assert first.kind == "wiki"
    assert first.article_id == "a1"
    assert first.title == "Priya Nair"
    assert first.match == "both"
    assert first.snippet == "<mark>Priya</mark>"
    # The note hits follow beneath the article.
    assert {r.kind for r in resp.results[1:]} == {"note"}


async def test_wiki_leg_groups_to_best_section_per_article() -> None:
    # One article, two matching sections → a single wiki hit (its best section).
    repo = FakeSearchRepo(
        wiki_dense=[whit("s1", article_id="a1"), whit("s2", article_id="a1")],
        wiki_fts=[whit("s1", article_id="a1")],
    )
    resp = await SearchService(repo, FakeEmbed()).search(CTX, "q", None, 20)
    wiki = [r for r in resp.results if isinstance(r, WikiSearchResult)]
    assert len(wiki) == 1


async def test_wiki_dense_skipped_when_embedding_degraded() -> None:
    repo = FakeSearchRepo(wiki_fts=[whit("s1", headline="<mark>hi</mark>")])
    resp = await SearchService(repo, FakeEmbed(fail=True)).search(CTX, "hi", None, 20)
    assert resp.degraded
    assert repo.wiki_dense_calls == 0  # dense wiki leg skipped, never errored
    wiki = [r for r in resp.results if isinstance(r, WikiSearchResult)]
    assert len(wiki) == 1
    assert wiki[0].match == "keyword"


def test_search_requires_q_and_clamps_limit(api: tuple[TestClient, StubSearchService]) -> None:
    client, stub = api
    assert client.get("/api/search").status_code == 422
    assert client.get("/api/search", params={"q": ""}).status_code == 422
    client.get("/api/search", params={"q": "x", "limit": 9999, "domain": "health"})
    assert stub.calls[-1] == ("x", "health", 100)
