"""`correct_fact` / `merge_entities` against real Postgres — the on-reply writes.

W3 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md. The LLM is faked (CLAUDE.md #5);
everything that decides how a write LANDS is real, because the design claim of both
tools is that they add no second write path. `correct_fact` is `commit_facts` with one
extra flag, and `merge_entities` stages the node op the shipped enact already runs.

What each test is defending:

- **the correction actually out-argues the graph** (D11). `ExtractedFact.correction` is
  what `supersession.decide()` reads to supersede every current head and commit the new
  value active + PINNED regardless of temporal order, and pinned is what stops a later
  note flipping it back. Asserted on the ROWS, not on the result text, because the
  result text is the model's window and the rows are the fact;
- **the identity key, not a fact id** — including the multi-row case, where the handler
  mints `f1`/`f2`, writes NOTHING, and the model retries. That is the whole reason
  `read_entity` does not need a v5;
- **the fold is staged and never enacted** (constraint 12). A note conversation is
  domain-narrowed by construction, `merge_entity_pair` asks Postgres
  `app.is_full_owner()` before any statement, and a half-completed cross-domain fold
  leaves facts stranded on a tombstone. So: a Proposal exists, and both entities are
  still live and unrepointed;
- **a tombstoned id is followed to its survivor** (`entities.live_entity_by_id`) rather
  than folded onto again — the shape `analysis/repo.resolve_review`'s merge-accept arm
  still reaches, and the reason its shape is not copied here;
- **the turn's own read scope is the ceiling on what either tool can be pointed at.**
  The write session runs at full owner scope (constraint 2), so the gate has to be on
  the ADDRESS: an entity the conversation cannot read is an entity it cannot correct.
"""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.replytools import CORRECT_FACT, MERGE_ENTITIES, build_reply_write_handlers
from jbrain.agent.session import AgentSessionRepo
from jbrain.analysis.entities import merge_entity_pair, normalize_alias
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.analysis import Entity, Fact
from jbrain.models.note_conversation import NoteConversationRepo, note_body_sha
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import (  # noqa: F401
    ingest,
    make_note,
    maker,
)
from tests.integration.test_note_conversation_rls import owner_ctx  # noqa: F401
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

BODY = (
    "Talked to Jeff about the move. He said he lives at 118 Pine Ave now, and that "
    "Kaiya is seen by Dr. Patel."
)


def _router() -> LlmRouter:
    return LlmRouter({"xai": FakeLlmClient()}, {"note.extract": ("xai", "grok-4.3")})


def _handlers(maker) -> dict[str, Any]:  # noqa: F811
    """The two handlers as `readtools.build_registry` wires them — the same repos the
    chat registry passes, so nothing here is a private path into the write."""
    return build_reply_write_handlers(
        maker,
        ProposalRepo(maker),
        SqlAnalysisRepo(maker),
        SqlNotesRepo(maker),
        _router(),
    )


async def _conversation(maker, owner: SessionContext, note_id: str) -> str:  # noqa: F811
    """A real note conversation: an `agent_sessions` row under the note persona plus the
    `note_conversations` row that gives it a note. Both tools find their note through
    THIS, never through an argument."""
    async with scoped_session(maker, owner) as s:
        session = await AgentSessionRepo(maker).create_on(
            s, owner, domain_scopes=["general"], title="note", agent="note_ingest"
        )
        note = await SqlNotesRepo(maker).get_note(owner, note_id)
        assert note is not None
        await NoteConversationRepo().start(
            s, session_id=session.id, note_id=note_id, body_sha=note_body_sha(note.body)
        )
    return session.id


def _ctx(owner: SessionContext, session_id: str, scopes: tuple[str, ...] = ("general",)):
    """The turn as `/chat` builds it for a note thread: the owner NARROWED to the
    conversation's own scopes (constraint 2's `(note_domain, 'general')`)."""
    narrowed = SessionContext(
        principal_id=owner.principal_id,
        principal_kind="owner",
        owner_scoped=True,
        domain_scopes=scopes,
    )
    return ToolContext(session=narrowed, scopes=scopes, agent_session_id=session_id)


async def _entity(maker, name: str, *, domain: str = "general", kind: str = "Person") -> str:  # noqa: F811
    """A confirmed entity with its canonical alias, so `find_entity`-shaped name lookup
    reaches it the way the model's own addressing does."""
    eid = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                " VALUES (:id, :kind, :name, 'confirmed', :domain)"
            ),
            {"id": str(eid), "kind": kind, "name": name, "domain": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_aliases (id, entity_id, alias, alias_norm, domain_code)"
                " VALUES (:aid, :id, :a, :norm, :domain)"
            ),
            {
                "aid": str(uuid.uuid4()),
                "id": str(eid),
                "a": name,
                "norm": normalize_alias(name),
                "domain": domain,
            },
        )
    return str(eid)


async def _fact(
    maker,  # noqa: F811
    entity_id: str,
    note_id: str,
    *,
    predicate: str,
    statement: str,
    value: str | None = None,
    object_id: str | None = None,
    kind: str = "attribute",
) -> str:
    fid = uuid.uuid4()
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, note_id, predicate, qualifier, kind,"
                " statement, value_json, object_entity_id, assertion, status, domain_code,"
                " reported_at, temporal_precision, extractor, prompt_version)"
                " VALUES (:id, :eid, :nid, :p, '', :k, :st, CAST(:v AS jsonb), :oid,"
                " 'asserted', 'active', 'general', now(), 'unknown', 'test', 'test-v1')"
            ),
            {
                "id": str(fid),
                "eid": entity_id,
                "nid": note_id,
                "p": predicate,
                "k": kind,
                "st": statement,
                "v": json.dumps({"value": value}) if value is not None else None,
                "oid": object_id,
            },
        )
    return str(fid)


async def _rows(maker, entity_id: str, predicate: str) -> list[Any]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return list(
            (
                await s.execute(
                    select(Fact).where(
                        Fact.entity_id == uuid.UUID(entity_id), Fact.predicate == predicate
                    )
                )
            )
            .scalars()
            .all()
        )


# --- correct_fact -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_correction_force_supersedes_the_head_and_pins_the_new_value(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """D11, on the rows. The old head is superseded (kept as history, never deleted —
    R1: retraction is not a verb), and the new one is `pinned`, which is what stops a
    later non-correction note flipping it back."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins")
    old = await _fact(
        maker,
        jeff,
        note_id,
        predicate="homeLocation",
        statement="Jeff lives at 118 Pine Ave.",
        value="118 Pine Ave",
        kind="state",
    )

    out = await _handlers(maker)[CORRECT_FACT](
        {
            "entity": jeff,
            "predicate": "homeLocation",
            "qualifier": "",
            "object": "412 Oak St",
            "statement": "Jeff lives at 412 Oak St.",
            "when": "",
            "replaces": "",
        },
        _ctx(owner_ctx, session_id),
    )
    body = str(out)
    # The result names what the SERVER did unasked — the model's only window into
    # `decide()` (TOOL_SURFACE, "Result shapes are the ACI").
    assert "412 Oak St" in body
    assert "kept as history" in body
    assert "correct_fact: 5 calls left this note" in body

    rows = {str(f.id): f for f in await _rows(maker, jeff, "homeLocation")}
    assert rows[old].status == "superseded"
    live = [f for f in rows.values() if f.status == "active"]
    assert len(live) == 1
    assert live[0].statement == "Jeff lives at 412 Oak St."
    # PINNED. The whole point of D11's flag surviving the correction-note retirement.
    assert live[0].pinned is True
    # Sourced to the conversation's OWN note — the note whose reading Jeff corrected.
    assert str(live[0].note_id) == note_id
    # And the tool reported a real fact write, so the D3 chip and the 0191 ledger both
    # see it (`facts` is the channel `converse.ledger_rows` folds into `touched`).
    assert isinstance(out, ToolOutput) and len(out.facts) == 1


@pytest.mark.asyncio
async def test_a_correction_at_an_empty_address_records_and_pins_anyway(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """Jeff saying "no, it's X" when nothing is on file is still Jeff saying X. Recording
    it unpinned would leave the next note free to overwrite the thing he just told us."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins")

    await _handlers(maker)[CORRECT_FACT](
        {
            "entity": "Jeff Hopkins",  # by NAME, not id — the note's own words
            "predicate": "homeLocation",
            "qualifier": "",
            "object": "412 Oak St",
            "statement": "Jeff lives at 412 Oak St.",
            "when": "",
            "replaces": "",
        },
        _ctx(owner_ctx, session_id),
    )
    live = [f for f in await _rows(maker, jeff, "homeLocation") if f.status == "active"]
    assert len(live) == 1
    assert live[0].pinned is True


@pytest.mark.asyncio
async def test_a_multi_row_key_mints_handles_and_writes_nothing(maker, tmp_path, owner_ctx) -> None:  # noqa: F811
    """The addressing design. `(entity, predicate, qualifier)` does not name one fact
    when the predicate is set-valued — each distinct object is a co-equal live edge — so
    the handler lists them as `f1`/`f2` and refuses, and the model retries. Nothing is
    written on the refusal: a guess here is the wrong fact corrected forever."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    jeff = await _entity(maker, "Jeff Hopkins")
    civic = await _entity(maker, "the Civic", kind="Thing")
    kayak = await _entity(maker, "the kayak", kind="Thing")
    for obj, label in ((civic, "Civic"), (kayak, "kayak")):
        await _fact(
            maker,
            jeff,
            note_id,
            predicate="owns",
            statement=f"Jeff owns the {label}.",
            object_id=obj,
            kind="relationship",
        )

    args = {
        "entity": jeff,
        "predicate": "owns",
        "qualifier": "",
        "object": "a bicycle",
        "statement": "Jeff owns a bicycle.",
        "when": "",
        "replaces": "",
    }
    out = str(await _handlers(maker)[CORRECT_FACT](args, _ctx(owner_ctx, session_id)))
    assert "holds 2 live values at once" in out
    assert "f1" in out and "f2" in out
    assert "Civic" in out and "kayak" in out
    assert "Nothing was changed" in out
    # Nothing written: still exactly the two edges it started with.
    assert len(await _rows(maker, jeff, "owns")) == 2

    # A handle the listing never minted is refused rather than guessed at.
    bad = str(
        await _handlers(maker)[CORRECT_FACT](
            {**args, "replaces": "f9"}, _ctx(owner_ctx, session_id)
        )
    )
    assert "no such handle" in bad
    assert len(await _rows(maker, jeff, "owns")) == 2

    # And the retry naming one of them proceeds.
    ok = str(
        await _handlers(maker)[CORRECT_FACT](
            {**args, "replaces": "f1"}, _ctx(owner_ctx, session_id)
        )
    )
    assert "no such handle" not in ok
    assert len(await _rows(maker, jeff, "owns")) == 3


@pytest.mark.asyncio
async def test_the_turns_read_scope_is_the_ceiling_on_what_can_be_corrected(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """The write session runs at FULL owner scope (constraint 2 — a floored fact write is
    refused by RLS outright, and layer-1 resolution carries no domain predicate), so the
    firewall cannot live there. It lives on the ADDRESS: the entity is resolved under the
    TURN's own scopes, so the only rows this tool can be pointed at are the ones
    `find_entity`/`read_entity` could already have shown it."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    clinical = await _entity(maker, "Dr. Patel", domain="health")

    out = str(
        await _handlers(maker)[CORRECT_FACT](
            {
                "entity": clinical,
                "predicate": "worksAt",
                "qualifier": "",
                "object": "Kaiser",
                "statement": "Dr. Patel works at Kaiser.",
                "when": "",
                "replaces": "",
            },
            _ctx(owner_ctx, session_id),  # general-scoped, as a general note's thread is
        )
    )
    assert "not an entity this conversation can see" in out
    assert not await _rows(maker, clinical, "worksAt")


# --- merge_entities -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fold_is_staged_and_nothing_is_folded(maker, tmp_path, owner_ctx) -> None:  # noqa: F811
    """Constraint 12. The tool raises a Proposal tied to this conversation's session (so
    it lands on the review inbox's notes tab beside its own thread) and leaves the graph
    exactly as it found it — both entities live, no tombstone, nothing repointed."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    a = await _entity(maker, "Dana Whitfield")
    b = await _entity(maker, "Dana W")

    out = await _handlers(maker)[MERGE_ENTITIES](
        {"entity_a": a, "entity_b": "Dana W", "reason": "Jeff says they are one person."},
        _ctx(owner_ctx, session_id),
    )
    body = str(out)
    assert "Staged" in body
    assert "not allowed to make on its own" in body
    assert "do not say they are merged" in body.lower()
    assert isinstance(out, ToolOutput) and out.proposal is not None
    assert out.proposal.kind == "merge"

    # Nothing folded: the enact is Jeff's, and it is a full-owner session.
    async with scoped_session(maker, SYSTEM_CTX) as s:
        statuses = {
            row.status
            for row in (
                await s.execute(
                    select(Entity.status, Entity.merged_into_id).where(
                        Entity.id.in_([uuid.UUID(a), uuid.UUID(b)])
                    )
                )
            ).all()
        }
    assert statuses == {"confirmed"}

    # The staged card carries the two ids structurally and asserts NO direction — the
    # survivor is `plan_merge`'s at enact, never the model's.
    row, nodes = await ProposalRepo(maker).load(owner_ctx, out.proposal.proposal_id)
    node = nodes[0]
    assert node.op == "merge_entities"
    assert {node.preview["entity_a"], node.preview["entity_b"]} == {a, b}
    assert "keep" not in node.preview and "survivor" not in node.preview
    # Tied to the thread that raised it, which is what puts it on the notes tab.
    assert row.session_id == session_id


@pytest.mark.asyncio
async def test_the_narrowed_conversation_could_not_have_folded_even_if_it_tried(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """Why staging is the only shape available, rather than a policy choice: the fold
    itself fails closed on the very session the reply turn runs under. This is the
    guard the tool is built around, asserted at the layer that enforces it."""
    from jbrain.analysis.entities import MergeScopeError

    a = await _entity(maker, "Dana Whitfield")
    b = await _entity(maker, "Dana W")
    narrowed = SessionContext(
        principal_id=owner_ctx.principal_id,
        principal_kind="owner",
        owner_scoped=True,
        domain_scopes=("general",),
    )
    async with scoped_session(maker, narrowed) as s:
        with pytest.raises(MergeScopeError):
            await merge_entity_pair(s, keep=uuid.UUID(a), gone=uuid.UUID(b))


@pytest.mark.asyncio
async def test_an_already_folded_pair_is_one_entity_not_a_second_fold(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """`entities.live_entity_by_id` follows `merged_into_id` to the survivor, so a
    tombstoned id resolves to what it became. Both ids landing on the same live row is
    "already the same entity" — never a second card that would fold onto a tombstone."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    keep = await _entity(maker, "Dana Whitfield")
    gone = await _entity(maker, "Dana W")
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await merge_entity_pair(s, keep=uuid.UUID(keep), gone=uuid.UUID(gone))

    out = str(
        await _handlers(maker)[MERGE_ENTITIES](
            {"entity_a": keep, "entity_b": gone, "reason": ""},
            _ctx(owner_ctx, session_id),
        )
    )
    assert "already the same entity" in out


@pytest.mark.asyncio
async def test_a_pair_jeff_already_called_distinct_is_refused_not_staged(  # noqa: F811
    maker,  # noqa: F811
    tmp_path,
    owner_ctx,  # noqa: F811
) -> None:
    """A rejected merge writes a permanent `distinct_from`, and the enact refuses on it.
    Checking here means Jeff is not handed a card whose only outcome is a refusal of a
    question he already answered."""
    note_id = await make_note(maker, domain="general", body=BODY)
    await ingest(maker, note_id, tmp_path)
    session_id = await _conversation(maker, owner_ctx, note_id)
    a = await _entity(maker, "Dana Whitfield")
    b = await _entity(maker, "Dana Wu")
    lo, hi = sorted((uuid.UUID(a), uuid.UUID(b)))
    async with scoped_session(maker, SYSTEM_CTX) as s:
        await s.execute(
            text(
                "INSERT INTO app.entity_distinctions (id, entity_a, entity_b, reason,"
                " domain_code) VALUES (:id, :a, :b, 'Jeff said so', 'general')"
            ),
            {"id": str(uuid.uuid4()), "a": str(lo), "b": str(hi)},
        )

    out = str(
        await _handlers(maker)[MERGE_ENTITIES](
            {"entity_a": a, "entity_b": b, "reason": ""}, _ctx(owner_ctx, session_id)
        )
    )
    assert "different people or things" in out
    assert "Nothing was staged" in out
