"""GrowBot brain tools (api/growbot_brain.py): the gate on every call (a phone the owner turned
brain access on for, holding the persona now), the owner's unscoped context behind it, the
short dated lines a search answers with, a note filed like a PWA note, and the call log row
each call leaves. Fakes for the personas, search, notes and the log: no database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import growbot_brain
from jbrain.api.internal_auth import require_growbot
from jbrain.db.session import SessionContext
from jbrain.growbot.calls import CallRecord
from jbrain.notes.service import UnknownDomain
from jbrain.search.service import SearchResponse, SearchResult, WikiSearchResult

PID = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
PHONE = "phone-aaaaaaaaaaaaaaaa"
OTHER = "phone-bbbbbbbbbbbbbbbb"
OWNER_ID = "7a1b2c3d-0000-4000-8000-000000000001"


class _Personas:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    async def brain_ok(self, ctx: SessionContext, pid: str, phone_id: str) -> bool:
        self.asked.append((pid, phone_id))
        return pid == PID and phone_id == PHONE


def _note(snippet: str, day: int = 3) -> SearchResult:
    return SearchResult(
        note_id="n1",
        chunk_id="c1",
        snippet=snippet,
        match="both",
        score=1.0,
        domain="health",
        destination=None,
        created_at=datetime(2026, 11, day, tzinfo=UTC),
        body_preview="the preview",
        attachment_count=0,
        source_kind="note",
        source_anchor=None,
    )


class _Search:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.asked: list[tuple[SessionContext, str, str | None, int]] = []

    async def search(
        self, ctx: SessionContext, q: str, domain: str | None, limit: int
    ) -> SearchResponse:
        self.asked.append((ctx, q, domain, limit))
        if self.fail:
            raise RuntimeError("embedder down")
        wiki = WikiSearchResult(
            article_id="a1",
            title="Furnace",
            blurb="The furnace filter is 16x25x1.",
            entity_kind="thing",
            domain="general",
            snippet="",
            match="both",
            score=2.0,
        )
        long = "word " * 80
        return SearchResponse(
            degraded=False,
            results=[wiki, _note("Saw the <b>hematologist</b>, all fine."), _note(long), _note("")]
            + [_note("five"), _note("six")],
        )


class _Notes:
    def __init__(self, created: bool = True, unknown: bool = False) -> None:
        self.created = created
        self.unknown = unknown
        self.made: list[tuple[SessionContext, dict[str, Any]]] = []

    async def create_note(self, ctx: SessionContext, **kw: Any) -> tuple[Any, bool]:
        if self.unknown:
            raise UnknownDomain("general")
        self.made.append((ctx, kw))
        return SimpleNamespace(id="note-1", domain=kw["domain"]), self.created


class _Log:
    def __init__(self) -> None:
        self.rows: list[CallRecord] = []

    async def record(self, ctx: SessionContext, rec: CallRecord) -> None:
        self.rows.append(rec)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    owner: dict[str, str | None] = {"id": OWNER_ID}
    events: list[dict[str, Any]] = []

    async def owner_id(maker: Any) -> str | None:
        return owner["id"]

    async def emit(maker: Any, ctx: SessionContext, **kw: Any) -> None:
        events.append({"ctx": ctx, **kw})

    monkeypatch.setattr(growbot_brain, "owner_principal_id", owner_id)
    monkeypatch.setattr(growbot_brain.wf_events, "emit_event", emit)
    app = FastAPI()
    app.include_router(growbot_brain.internal_router, prefix="/internal")
    app.dependency_overrides[require_growbot] = lambda: None
    w = SimpleNamespace(
        app=app,
        owner=owner,
        events=events,
        personas=_Personas(),
        search=_Search(),
        notes=_Notes(),
        log=_Log(),
    )
    app.state.session_maker = object()
    app.state.growbot_personas = w.personas
    app.state.search_service = w.search
    app.state.notes_repo = w.notes
    app.state.growbot_calls = w.log
    w.client = TestClient(app)
    return w


def _settle() -> None:
    asyncio.run(asyncio.sleep(0))


WHO = {"persona": PID, "phone": PHONE}


@pytest.mark.parametrize("route", ["search", "note"])
@pytest.mark.parametrize(
    ("who", "status"),
    [
        ({"persona": PID, "phone": "x"}, 401),
        ({"persona": "p" * 65, "phone": PHONE}, 401),
        ({"persona": PID, "phone": OTHER}, 403),
        ({"persona": "not-a-uuid", "phone": PHONE}, 403),
    ],
)
def test_only_the_phone_the_owner_chose_gets_through(
    world: SimpleNamespace, route: str, who: dict[str, str], status: int
) -> None:
    r = world.client.post(f"/internal/growbot/brain/{route}", json={**who, "q": "x", "text": "x"})
    assert r.status_code == status
    assert world.search.asked == [] and world.notes.made == [] and world.log.rows == []


@pytest.mark.parametrize("route", ["search", "note"])
def test_a_box_with_no_owner_lets_nothing_through(world: SimpleNamespace, route: str) -> None:
    world.owner["id"] = None
    r = world.client.post(f"/internal/growbot/brain/{route}", json={**WHO, "q": "x", "text": "x"})
    assert r.status_code == 403 and r.json() == {"detail": "off"}


def test_the_route_needs_the_growbot_token() -> None:
    assert any(
        getattr(d, "dependency", None) is require_growbot
        for d in growbot_brain.internal_router.dependencies
    )


def test_a_search_runs_as_the_owner_across_every_domain_and_answers_short(
    world: SimpleNamespace,
) -> None:
    """Owner, 2026-11-13: "searching notes should not be domain restricted with owner key"."""
    r = world.client.post("/internal/growbot/brain/search", json={**WHO, "q": "  furnace  filter "})
    assert r.status_code == 200
    [(ctx, q, domain, limit)] = world.search.asked
    assert ctx == SessionContext(principal_id=OWNER_ID, principal_kind="owner")
    assert not ctx.owner_scoped and ctx.domain_scopes == ()
    assert (q, domain, limit) == ("furnace filter", None, growbot_brain.HITS)
    got = r.json()
    lines = got["text"].split("\n")
    assert got["n"] == 4 == len(lines)
    assert lines[0] == "- (wiki: Furnace) The furnace filter is 16x25x1."
    assert lines[1] == "- (2026-11-03) Saw the hematologist, all fine."
    assert lines[2].endswith("…") and len(lines[2]) < growbot_brain.HIT_CHARS + 20
    assert lines[3] == "- (2026-11-03) the preview"
    _settle()
    [row] = world.log.rows
    assert (row.kind, row.ok, row.model, row.persona_id) == ("brain", True, "search", PID)
    assert row.request == {"asked": "furnace filter"} and row.reply == got["text"]


def test_an_empty_search_and_a_search_that_fails(world: SimpleNamespace) -> None:
    assert world.client.post("/internal/growbot/brain/search", json=WHO).status_code == 400
    world.search.fail = True
    r = world.client.post("/internal/growbot/brain/search", json={**WHO, "q": "x"})
    assert r.status_code == 503
    _settle()
    [row] = world.log.rows
    assert not row.ok and row.error == "RuntimeError"


def test_a_note_is_filed_like_a_pwa_note_and_marked_as_the_creatures(
    world: SimpleNamespace,
) -> None:
    r = world.client.post(
        "/internal/growbot/brain/note", json={**WHO, "text": "  Furnace filter is 16x25.  "}
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    [(ctx, kw)] = world.notes.made
    assert ctx.principal_id == OWNER_ID and not ctx.owner_scoped
    assert kw["body"] == "Furnace filter is 16x25." and kw["domain"] == "general"
    assert kw["provenance"] == "agent" and kw["source_ref"] == f"growbot:{PID}"
    assert kw["client_id"].startswith("growbot-")
    [ev] = world.events
    assert ev["payload"] == {"note_id": "note-1"} and ev["principal_id"] == OWNER_ID
    _settle()
    [row] = world.log.rows
    assert (row.kind, row.model, row.reply) == ("brain", "note", "note-1")


def test_a_replayed_note_starts_no_second_ingest(world: SimpleNamespace) -> None:
    world.notes.created = False
    assert world.client.post("/internal/growbot/brain/note", json={**WHO, "text": "x"}).json() == {
        "ok": True
    }
    assert world.events == []


def test_an_empty_note_and_one_the_box_cannot_file(world: SimpleNamespace) -> None:
    assert world.client.post("/internal/growbot/brain/note", json=WHO).status_code == 400
    world.notes.unknown = True
    r = world.client.post("/internal/growbot/brain/note", json={**WHO, "text": "x"})
    assert r.status_code == 503


def test_a_creature_filing_note_after_note_is_stopped(world: SimpleNamespace) -> None:
    """Security review, 2026-11-13: a looping or prompt-injected creature could flood the notes
    and the ingest behind them. Six in ten minutes per persona, then a refusal it can own up to."""
    codes = [
        world.client.post("/internal/growbot/brain/note", json={**WHO, "text": f"n{i}"}).status_code
        for i in range(7)
    ]
    assert codes == [200] * 6 + [429]
    assert len(world.notes.made) == 6
    assert world.log.rows[-1].kind == "brain"
