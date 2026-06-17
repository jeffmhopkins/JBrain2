"""The wiki-editorial WRITE tools (file_correction / request_rebuild / add_source_exclusion):
they mint the sanctioned inputs (correction note, rebuild, exclusion) and defer jobs — with fakes
for the notes repo + job queue (the exclusion's DB write is covered in the integration suite)."""

from typing import Any

from jbrain.agent.loop import ToolContext
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.db.session import SessionContext
from jbrain.notes.service import NoteInfo, UnknownDomain

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=("general",))


class FakeNotes:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_unknown_domain = False

    async def create_note(self, ctx, **kw) -> tuple[NoteInfo, bool]:
        if self.raise_unknown_domain:
            raise UnknownDomain("nope")
        self.calls.append(kw)
        note = NoteInfo(
            id="note-1",
            client_id=kw["client_id"],
            domain=kw["domain"],
            destination=None,
            body=kw["body"],
            created_at=None,  # type: ignore[arg-type]
            ingest_state="pending",
            provenance=kw.get("provenance", "human"),
        )
        return note, True


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(self, ctx, kind, payload, **kw) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


def _handlers() -> tuple[dict, FakeNotes, FakeJobs]:
    notes, jobs = FakeNotes(), FakeJobs()
    return build_wiki_write_handlers(notes, jobs, object()), notes, jobs  # type: ignore[arg-type]


async def test_file_correction_mints_owner_correction_and_drives_ingest() -> None:
    handlers, notes, jobs = _handlers()
    out = await handlers["file_correction"](
        {"body": "She works at Globex.", "domain": "general", "article_id": "a1"}, CTX
    )
    assert "correction" in out.lower()
    call = notes.calls[-1]
    assert call["provenance"] == "owner_correction"
    assert call["source_ref"] == "wiki:a1"
    assert call["client_id"].startswith("correction-")
    assert jobs.enqueued == [("ingest_note", {"note_id": "note-1"})]


async def test_file_correction_needs_body_and_domain() -> None:
    handlers, notes, jobs = _handlers()
    out = await handlers["file_correction"]({"body": "x"}, CTX)  # no domain
    assert "needs" in out.lower()
    assert not notes.calls and not jobs.enqueued


async def test_file_correction_unknown_domain_is_reported() -> None:
    handlers, notes, jobs = _handlers()
    notes.raise_unknown_domain = True
    out = await handlers["file_correction"]({"body": "x", "domain": "bogus"}, CTX)
    assert "not a known domain" in out
    assert not jobs.enqueued


async def test_request_rebuild_enqueues_the_article() -> None:
    handlers, _, jobs = _handlers()
    out = await handlers["request_rebuild"]({"article_id": "a1"}, CTX)
    assert "rebuild" in out.lower()
    assert jobs.enqueued == [("wiki_rebuild", {"target": "a1"})]


async def test_request_rebuild_needs_an_article_id() -> None:
    handlers, _, jobs = _handlers()
    out = await handlers["request_rebuild"]({}, CTX)
    assert "needs an article_id" in out
    assert not jobs.enqueued


async def test_add_source_exclusion_validates_before_touching_the_db() -> None:
    handlers, _, jobs = _handlers()
    # Missing domain, and a malformed id — both return before any DB write / enqueue.
    assert "needs a note_id" in await handlers["add_source_exclusion"]({"note_id": "n1"}, CTX)
    bad = await handlers["add_source_exclusion"](
        {"note_id": "not-a-uuid", "domain": "general"}, CTX
    )
    assert "valid ids" in bad
    assert not jobs.enqueued
