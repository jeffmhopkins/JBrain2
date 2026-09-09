"""The owner-correction note out-arguing the graph — on the CONVERSATION path.

W5's stated precondition (docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, "Port
`file_correction` first"). `docs/plans/PHASE6_WIKI_PLAN.md` §4 names
`arbiter.plan_intent(correction=True)` as the wiki correction loop's shipped exit
criterion — "the owner out-argues the wiki" — and `wiki/lint.py` offers it as a review
card action. Its whole bridge was two lines inside `analysis/pipeline.integrate_note`
reading `provenance == 'owner_correction'`, and W5a deletes that function together with
the arbiter. These tests are the evidence that the same criterion holds without them.

The LLM is faked (CLAUDE.md #5). Everything that decides whether the criterion holds is
real: the `file_correction` handler that mints the note, `ingest_note`, the production
`note_converse` wiring (`executor_for_note`, the per-note registry, the real loop),
`commit_facts` and `supersession.decide()`.

What each test defends:

- **an owner correction still force-supersedes and PINS**, end to end from the wiki
  lever, with no arbiter anywhere in the path;
- **it is authoritative only for what the note LITERALLY STATES.** The arbiter's
  `fact_correction = correction and signals_i.surface_attested` had a refusing half, and
  a port that dropped it would let a hallucinated value buy the most destructive write
  in the system;
- **the pin is what makes it out-argue the FUTURE**, not just the past — a later
  ordinary note cannot flip a corrected head;
- **an ordinary note in the same shape does none of this**, so the elevation is a
  property of the provenance and not of the write path drifting.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.agent.graphwritetools import NoteGraphWriter, NoteTarget
from jbrain.agent.loop import ToolContext
from jbrain.agent.wikiwritetools import build_wiki_write_handlers
from jbrain.analysis.converse import note_converse_handler
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, ToolCall
from jbrain.models.analysis import Fact
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import SYSTEM_CTX, PgJobQueue
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_note_conversation_rls import owner_ctx
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The head the wiki article would be built from, and the correction that disputes it.
# One unique person per run: resolution is BY NAME against a shared fixture database.
PRIOR = "Coffee with {name} at Ritual. She works for Everlane as a staff engineer."
CORRECTION = "{name} does not work for Everlane. She works for Kestrel Labs."


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def owner(maker: async_sessionmaker[AsyncSession]) -> SessionContext:
    return await owner_ctx(maker)


def _const(value: str):  # noqa: ANN202
    async def _get() -> str:
        return value

    return _get


def _router(*turns: LlmTurn) -> LlmRouter:
    """A model scripted turn by turn. `FakeLlmClient` raises when the loop asks for one
    more than it was given, so an extra turn is a loud failure rather than a silent
    fallback to whatever the fake would otherwise say."""
    return LlmRouter(
        {"xai": FakeLlmClient(turns=list(turns))},
        {"agent.turn": ("xai", "grok-4.3"), "note.extract": ("xai", "grok-4.3")},
    )


def _call(idx: int, name: str, arguments: dict[str, Any]) -> LlmTurn:
    return LlmTurn("", (ToolCall(f"c{idx}", name, arguments),), "tool_use", LlmUsage(10, 3))


async def _ingest(maker: async_sessionmaker[AsyncSession], note_id: str, tmp_path: Any) -> None:
    """Chunk the note. The span check reads the note's CHUNKS, so an un-ingested
    correction note attests nothing and the elevation would never fire — which is a
    property worth having the test depend on rather than route around."""
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})


async def _plain_note(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, body: str, tmp_path: Any
) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        owner, client_id=f"corr-{uuid.uuid4()}", domain="general", destination=None, body=body
    )
    await _ingest(maker, note.id, tmp_path)
    return note.id


async def _correction_note(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, body: str, tmp_path: Any
) -> str:
    """A correction minted through the REAL wiki lever — the `file_correction` tool the
    Talk thread and the lint card's `correct` verb both reach. Going through the handler
    rather than writing the provenance by hand is the point: what this file claims is
    that the shipped lever still lands, so the lever has to be in the path."""
    handlers = build_wiki_write_handlers(SqlNotesRepo(maker), PgJobQueue(maker), maker)
    out = await handlers["file_correction"](
        {"body": body, "domain": "general", "article_id": str(uuid.uuid4())},
        ToolContext(session=owner, scopes=("general",), agent_session_id=None),
    )
    assert "Filed your correction" in str(out)
    async with scoped_session(maker, owner) as s:
        note_id, provenance = (
            await s.execute(
                text(
                    "SELECT id::text, provenance FROM app.notes"
                    " WHERE provenance = 'owner_correction' ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).one()
    assert provenance == "owner_correction"
    await _ingest(maker, str(note_id), tmp_path)
    return str(note_id)


async def _seed_head(
    maker: async_sessionmaker[AsyncSession],
    owner: SessionContext,
    tmp_path: Any,
    name: str,
) -> tuple[str, uuid.UUID]:
    """The fact the article would cite, written through the ordinary conversation path so
    the incumbent is a genuine attested head rather than a hand-inserted row."""
    note_id = await _plain_note(maker, owner, PRIOR.format(name=name), tmp_path)
    writer = NoteGraphWriter(
        maker,
        AnalysisPipeline(maker, _router()),
        target=await _target(maker, note_id, provenance="human"),
        write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
        read_scopes=("general",),
    )
    ctx = ToolContext(session=owner, scopes=("general",), agent_session_id=None)
    await writer.resolve_entity({"entities": [{"surface": name, "kind": "person"}]}, ctx)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "worksFor",
                    "object": "Everlane",
                    "statement": f"{name} works for Everlane.",
                    "when": "",
                    "when_end": "",
                    "quote": "She works for Everlane as a staff engineer.",
                    "confidence": 1,
                }
            ]
        },
        ctx,
    )
    head = uuid.UUID(out.facts[0].fact_id)
    row = await _fact(maker, head)
    assert (row.status, row.pinned) == ("active", False)
    return note_id, head


async def _target(
    maker: async_sessionmaker[AsyncSession], note_id: str, *, provenance: str
) -> NoteTarget:
    from jbrain.models.notes import Note

    async with scoped_session(maker, SYSTEM_CTX) as s:
        row = (
            await s.execute(
                select(Note.created_at, Note.tz_offset_minutes).where(Note.id == note_id)
            )
        ).one()
    return NoteTarget(
        note_id=uuid.UUID(note_id),
        domain="general",
        captured_at=row.created_at,
        tz_offset_minutes=row.tz_offset_minutes,
        provenance=provenance,
    )


async def _fact(maker: async_sessionmaker[AsyncSession], fact_id: uuid.UUID) -> Any:
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (await s.execute(select(Fact).where(Fact.id == fact_id))).scalar_one()


async def _head_for(maker: async_sessionmaker[AsyncSession], note_id: str) -> Any:
    """The one `worksFor` row this note wrote."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                select(Fact).where(Fact.note_id == uuid.UUID(note_id), Fact.predicate == "worksFor")
            )
        ).scalar_one()


async def _run_conversation(
    maker: async_sessionmaker[AsyncSession],
    owner: SessionContext,
    note_id: str,
    name: str,
    *,
    quote: str,
) -> None:
    """The PRODUCTION `note_converse` handler over this note: `executor_for_note` builds
    the per-note registry and the writer, so the wiring under test is the one the worker
    runs, not a writer this test assembled."""
    router = _router(
        _call(1, "resolve_entity", {"entities": [{"surface": name, "kind": "person"}]}),
        _call(
            2,
            "assert_fact",
            {
                "facts": [
                    {
                        "subject": name,
                        "predicate": "worksFor",
                        "object": "Kestrel Labs",
                        "statement": f"{name} works for Kestrel Labs.",
                        "when": "",
                        "when_end": "",
                        "quote": quote,
                        "confidence": 1,
                    }
                ]
            },
        ),
        LlmTurn("Recorded your correction.", (), "end_turn", LlmUsage(10, 3)),
    )
    handler = note_converse_handler(maker, router, pipeline=AnalysisPipeline(maker, router))
    await handler({"note_id": note_id})


# --- the exit criterion -------------------------------------------------------


async def test_a_correction_note_force_supersedes_and_pins_with_no_arbiter_in_the_path(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, tmp_path: Any
) -> None:
    """PHASE6 §4's exit criterion, restated against the path that survives W5a.

    Nothing here calls `plan_intent`. The note is minted by `file_correction`, ingested,
    and read by the note conversation; the elevation is `NoteTarget.is_correction`, and
    what proves it is what happened to the incumbent — superseded, not merely accompanied
    — and to the new row, which is pinned."""
    name = f"Dana Corrected {uuid.uuid4().hex[:6]}"
    _, prior = await _seed_head(maker, owner, tmp_path, name)

    correction_id = await _correction_note(maker, owner, CORRECTION.format(name=name), tmp_path)
    await _run_conversation(
        maker, owner, correction_id, name, quote=f"{name} does not work for Everlane."
    )

    corrected = await _head_for(maker, correction_id)
    assert corrected.status == "active"
    # THE criterion: pinned, and at the full weight the arbiter gave it (`weight = 1.0 if
    # fact_correction`), not the model's self-report and not the inferred ceiling.
    assert corrected.pinned is True
    assert corrected.confidence == pytest.approx(1.0)

    # And the head the article was citing is gone from `current`, superseded by this one.
    was_head = await _fact(maker, prior)
    assert was_head.status != "active"
    assert was_head.superseded_by == corrected.id


async def test_an_inferred_fact_in_a_correction_note_is_not_elevated(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, tmp_path: Any
) -> None:
    """The arbiter's refusing half, ported with the rest of it.

    "A correction is authoritative only for what the note LITERALLY STATES" — a fact
    whose value the correction's own text does not attest follows the ordinary capped
    path. Dropping this would have made the port strictly more permissive than the thing
    it replaces: a hallucinated value inside a correction note would force-supersede and
    pin, which is the single most destructive write the system has."""
    name = f"Dana Inferred {uuid.uuid4().hex[:6]}"
    _, prior = await _seed_head(maker, owner, tmp_path, name)

    correction_id = await _correction_note(maker, owner, CORRECTION.format(name=name), tmp_path)
    # A quote the correction note does not contain — the model paraphrasing, which is the
    # likeliest failure and the one the span check exists for.
    await _run_conversation(
        maker, owner, correction_id, name, quote="she left Everlane for Kestrel last spring"
    )

    written = await _head_for(maker, correction_id)
    assert written.pinned is False
    assert written.confidence == pytest.approx(0.4)
    # The incumbent survives untouched: an unattested claim cannot out-argue anything,
    # correction note or not.
    was_head = await _fact(maker, prior)
    assert was_head.status == "active"
    assert was_head.superseded_by is None


async def test_the_pin_is_what_out_argues_the_next_note_as_well(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, tmp_path: Any
) -> None:
    """ "Out-argue the wiki" is a claim about the FUTURE, and the pin is the whole of it.

    A correction that only superseded the current head would be undone by the next note
    that happened to mention the old value — and on a machine-written wiki whose sources
    are the owner's own corpus, that is a routine event, not an edge case. `decide()`
    holds a candidate against a pinned head instead of flipping it, so the article keeps
    the corrected value until the owner says otherwise."""
    name = f"Dana Pinned {uuid.uuid4().hex[:6]}"
    await _seed_head(maker, owner, tmp_path, name)
    correction_id = await _correction_note(maker, owner, CORRECTION.format(name=name), tmp_path)
    await _run_conversation(
        maker, owner, correction_id, name, quote=f"{name} does not work for Everlane."
    )
    corrected = await _head_for(maker, correction_id)
    assert corrected.pinned is True

    # A LATER ordinary note, perfectly attested, and asserting a NEW value at the key the
    # owner pinned — the shape that would silently flip an unpinned head.
    later_body = f"Ran into {name} today. She works for Northwind now, apparently."
    later_id = await _plain_note(maker, owner, later_body, tmp_path)
    writer = NoteGraphWriter(
        maker,
        AnalysisPipeline(maker, _router()),
        target=await _target(maker, later_id, provenance="human"),
        write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
        read_scopes=("general",),
    )
    ctx = ToolContext(session=owner, scopes=("general",), agent_session_id=None)
    await writer.resolve_entity({"entities": [{"surface": name, "kind": "person"}]}, ctx)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "worksFor",
                    "object": "Northwind",
                    "statement": f"{name} works for Northwind.",
                    "when": "",
                    "when_end": "",
                    "quote": "She works for Northwind now, apparently.",
                    "confidence": 1,
                }
            ]
        },
        ctx,
    )

    still = await _fact(maker, corrected.id)
    assert still.status == "active"
    assert still.superseded_by is None
    # The later note is not silently dropped — it is recorded and NOT live, and the model
    # is told so, because a correction the owner has since changed his mind about has to
    # be findable.
    assert "held" in str(out)


async def test_the_same_write_off_an_ordinary_note_neither_pins_nor_is_told_it_did(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, tmp_path: Any
) -> None:
    """The control, and the one that fails if the elevation ever stops reading the
    provenance: the same body, the same tool call, the same quote — filed as an ordinary
    note. `human` is what the capture API stamps, and `CreateNoteRequest` has no field
    that could make it anything else."""
    name = f"Dana Ordinary {uuid.uuid4().hex[:6]}"
    _, prior = await _seed_head(maker, owner, tmp_path, name)

    plain_id = await _plain_note(maker, owner, CORRECTION.format(name=name), tmp_path)
    await _run_conversation(
        maker, owner, plain_id, name, quote=f"{name} does not work for Everlane."
    )

    written = await _head_for(maker, plain_id)
    assert written.pinned is False
    # It may well supersede on the ordinary temporal path — that is not what is being
    # asserted. What must not happen is the PIN, which is the authority the owner's own
    # correction buys and an ordinary note does not.
    was_head = await _fact(maker, prior)
    assert was_head.pinned is False


async def test_the_result_line_says_the_note_out_argued_what_was_on_file(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext, tmp_path: Any
) -> None:
    """D3: every write is legible, and a write that force-supersedes and pins is the one
    that most needs to be. The model's only window into `decide()` is the result line, so
    a correction that landed silently would be reported back as an ordinary fact — and
    the thread the owner reads is built from these lines.

    Asserted at the WRITER, because the text is what the handler returns and the
    conversation only stores a summary of it."""
    name = f"Dana Legible {uuid.uuid4().hex[:6]}"
    await _seed_head(maker, owner, tmp_path, name)
    correction_id = await _correction_note(maker, owner, CORRECTION.format(name=name), tmp_path)

    ctx = ToolContext(session=owner, scopes=("general",), agent_session_id=None)
    elevated = await _direct_write(maker, correction_id, name, ctx, provenance="owner_correction")
    assert "out-argues what was on file" in elevated
    assert "pinned against later notes" in elevated

    # The same call on an ordinary note says no such thing.
    plain_id = await _plain_note(maker, owner, CORRECTION.format(name=name), tmp_path)
    ordinary = await _direct_write(maker, plain_id, name, ctx, provenance="human")
    assert "out-argues" not in ordinary


async def _direct_write(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    name: str,
    ctx: ToolContext,
    *,
    provenance: str,
) -> str:
    writer = NoteGraphWriter(
        maker,
        AnalysisPipeline(maker, _router()),
        target=await _target(maker, note_id, provenance=provenance),
        write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
        read_scopes=("general",),
    )
    await writer.resolve_entity({"entities": [{"surface": name, "kind": "person"}]}, ctx)
    out = await writer.assert_fact(
        {
            "facts": [
                {
                    "subject": "e1",
                    "predicate": "worksFor",
                    "object": "Kestrel Labs",
                    "statement": f"{name} works for Kestrel Labs.",
                    "when": "",
                    "when_end": "",
                    "quote": f"{name} does not work for Everlane.",
                    "confidence": 1,
                }
            ]
        },
        ctx,
    )
    return str(out)


async def test_the_production_owner_resolver_hands_back_a_string(
    maker: async_sessionmaker[AsyncSession], owner: SessionContext
) -> None:
    """The bug this file's own end-to-end run found, pinned so it cannot come back.

    `note_converse_handler` resolves the owner through `tasks.scheduler
    ._owner_principal_id`, whose signature says `str | None` — but `app.principals.id` is
    a `uuid` column, so asyncpg handed back a `uuid.UUID` and the `SessionContext` built
    from it died in `scoped_session`'s `set_config` ("expected str, got UUID"). Every
    `note_converse` job on a real box failed there, before the conversation ever opened;
    nothing caught it because both other callers wrap the value in `str()` at their own
    call site (`tasks_tick`, `PlanContinuationRunner` — whose test comment literally reads
    "raw uuid, like production") and every note-conversation test injects the id itself.

    The wiki correction loop's exit criterion now runs through that handler, so the
    resolver returning the type it promises is part of what holds it up."""
    from jbrain.tasks.scheduler import _owner_principal_id

    resolved = await _owner_principal_id(maker)
    assert isinstance(resolved, str)
    assert resolved == owner.principal_id
