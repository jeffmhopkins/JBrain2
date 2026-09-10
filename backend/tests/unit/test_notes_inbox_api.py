"""The review inbox's notes tab (D4/D5 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md).

Two properties, and the first is the design:

- **The inbox only redirects.** Not as an intention of the screen, but as a property of
  the contract: the row shape carries a session to open and no item id, and this module
  is the whole notes-tab surface — there is nothing here to POST to. A test asserts that,
  so a later change that adds an answer endpoint has to delete a test to do it.
- The merge of the two producers (waiting conversations, staged approvals) is oldest
  first, so the tab drains from the top whichever one a row came from.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.agent.proposals import WaitingApproval
from jbrain.api import analysis
from jbrain.api.analysis import NotesInboxRow, merge_notes_inbox
from jbrain.api.deps import current_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.models.note_conversation import NoteConversationRepo, NotesInboxEntry
from tests.unit.fakes import FakeAuthRepo

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def entry(**over: object) -> NotesInboxEntry:
    base: dict[str, object] = {
        "session_id": "sess-1",
        "agent": "note_ingest",
        "note_id": "note-1",
        "domain": "health",
        "note_excerpt": "Started 10mg of the new one Tuesday.",
        "captured_at": NOW - timedelta(days=1),
        "questions": ["Which is “the new one”?"],
        "waiting_since": NOW - timedelta(hours=6),
        "committed": 2,
        "live": False,
    }
    return NotesInboxEntry(**(base | over))  # type: ignore[arg-type]


def approval(**over: object) -> WaitingApproval:
    base: dict[str, object] = {
        "id": "p1",
        "kind": "owner_prefs",
        "domain": "general",
        "title": "Stop splitting recipe ingredients.",
        "session_id": "sess-chat",
        "agent": "curator",
        "staged_at": NOW - timedelta(days=3),
    }
    return WaitingApproval(**(base | over))  # type: ignore[arg-type]


def test_the_row_carries_a_destination_and_nothing_to_decide_with() -> None:
    """The redirect is enforced by the SHAPE: no item id, no action, no choices. A screen
    cannot offer a decision it has no handle for."""
    fields = set(NotesInboxRow.model_fields)
    assert "session_id" in fields
    assert not fields & {"id", "item_id", "action", "actions", "choices", "proposals"}


def test_the_two_producers_interleave_oldest_first() -> None:
    rows = merge_notes_inbox(
        [
            entry(session_id="new", waiting_since=NOW - timedelta(hours=6)),
            entry(session_id="old", waiting_since=NOW - timedelta(days=9)),
        ],
        [approval()],
    )
    # 9 days, 3 days, 6 hours — the list drains from the top, and the staged approval
    # takes its place by age rather than being bucketed after the questions.
    assert [r.session_id for r in rows] == ["old", "sess-chat", "new"]
    assert [r.kind for r in rows] == ["question", "approval", "question"]


def test_a_staged_approval_says_what_it_is_in_the_server_s_words() -> None:
    (row,) = merge_notes_inbox([], [approval()])
    assert row.quote == "Stop splitting recipe ingredients."
    assert len(row.asks) == 1 and "standing instructions" in row.asks[0]
    assert row.note_id is None and row.committed == 0 and row.live is False
    # The persona the redirect must flip to before opening the thread.
    assert row.agent == "curator"


def test_a_first_pass_still_reading_is_listed_with_no_question() -> None:
    (row,) = merge_notes_inbox([entry(questions=[], live=True, committed=0)], [])
    assert row.live is True and row.asks == []


def test_the_whole_question_set_reaches_the_row() -> None:
    """R1c: one ask carries several questions, and the row is what tells the owner how
    many are waiting before they spend the tap. The row stays a pure redirect (D4) — the
    set is quoted, never answered here."""
    (row,) = merge_notes_inbox([entry(questions=["Which Sarah?", "Which dose?"])], [])
    assert row.asks == ["Which Sarah?", "Which dose?"]


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        yield test_client


def test_the_notes_tab_is_owner_only(client: TestClient) -> None:
    """Explicitly, not by the module's pre-P7 implicitness: the rows quote note bodies
    across every domain, health included, so a capability token must not reach them."""
    assert client.get("/api/review/notes").status_code == 401

    app = cast(FastAPI, client.app)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="cap-1", kind="capability_token", label="scoped"
    )
    try:
        assert client.get("/api/review/notes").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_the_notes_tab_serves_the_two_producers_as_one_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end over the route, with both reads faked: what the PWA receives is one
    ordered list of redirects, and the JSON carries no verb."""
    app = cast(FastAPI, client.app)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id="owner-1", kind="owner", label="owner"
    )

    @asynccontextmanager
    async def fake_scope(_maker: object, _ctx: object) -> AsyncIterator[object]:
        yield object()

    async def fake_notes_inbox(_self: object, _session: object, *, limit: int = 50) -> list[object]:
        return [entry(session_id="old", waiting_since=NOW - timedelta(days=9))]

    class FakeProposals:
        async def list_waiting_approvals(self, _ctx: object) -> list[WaitingApproval]:
            return [approval()]

    monkeypatch.setattr(analysis, "scoped_session", fake_scope)
    monkeypatch.setattr(NoteConversationRepo, "notes_inbox", fake_notes_inbox)
    app.state.session_maker = object()
    app.state.agent_proposals = FakeProposals()
    try:
        body = client.get("/api/review/notes")
        assert body.status_code == 200
        items = body.json()["items"]
        assert [i["kind"] for i in items] == ["question", "approval"]
        assert items[0]["session_id"] == "old" and items[0]["agent"] == "note_ingest"
        # Nothing in the payload addresses a decision.
        assert not set(items[0]) & {"id", "action", "choices"}
    finally:
        app.dependency_overrides.clear()
