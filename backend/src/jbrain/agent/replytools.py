"""The note-graph tools an owner's REPLY turn dispatches.

`correct_fact` and `merge_entities` — the two write verbs that need Jeff in the room —
plus the reply turn's copies of `resolve_entity`, `assert_fact` and `close_reading`,
which D8's on-reply allowlist has always named and which nothing had ever bound.
`close_reading` is R3's, and it is the one that is not an increment: the other four
add, correct or fold ONE thing, while this one states what the WHOLE NOTE says now —
the claim the settle is derived from (`AGENT_INGEST_REWRITE.md` §1).

W3 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, built to
docs/research/agent-ingest/TOOL_SURFACE.md's on-reply rows. D8 splits the note persona's
surface in two and `agents.agent_for_owner_reply` is the seam; these are the handlers
behind the half that only a turn the owner sent can reach.

**All five are bound on the CHAT registry, not on the worker's per-note one.** The
owner's reply into a note thread is an ordinary `/chat` turn, so that is the only
registry the reply turn consults — the same reason `ask_owner` is wired there. None
takes a note id: they find their conversation through `ToolContext.agent_session_id`,
and outside a note conversation all five refuse. A write primitive a hostile body could
point at another note is not a tool, it is a hole.

**`assert_fact` on the reply turn is a safety property, not a convenience.** Without it
the only write verb a reply turn holds is `correct_fact`, and a correction at an empty
address commits `insert_pinned=True` — so a new fact the owner states in passing ("and
her title is CTO") lands PINNED against every later note. The verb that records a new
fact has to be reachable in the turn that learns one.

**And it is reachable only on a turn whose words LANDED ON THE NOTE** (R3's review,
finding 2, re-keyed by its second round). What `clarify.record_owner_reply` appends as a
D6 clarification block becomes the note's own text, so a fact asserted beside it is
re-stated by the note's next reading and survives the sweep. Where the append did not
happen — a thread that is not waiting, an append that failed, a send whose free text
`_pair` had no open question for — the owner's words reach no note at all
(`note_clarifications.question` is NOT NULL, so D6 has no unprompted block), and the row
would be swept by the note's next unattended pass, which shares this producer's single
claim (`analysis/settle_owner.py`). `agents.narrow_for_unprompted_reply` takes the verb
off that turn, applied from `api/agent.py` on `clarify.owner_words_reached_note` — the
outcome of the append, which is the invariant, rather than the thread state, which is a
proxy that admits all three cases above.

**`correct_fact` is bound on that turn and its EMPTY-ADDRESS arm is not.** The verb is
two writes wearing one name: against a live head it supersedes and pins, which is the
owner fixing something wrong and must stay reachable whenever he is in the room; against
an address holding nothing it MINTS a pinned fact no later note can supersede. On a turn
whose words the note never received the second is strictly worse than the loss it would
prevent — permanent and unfalsifiable, since no reading of a note that does not say it
can correct it — so the handler refuses that arm when `assert_fact` is not in this turn's
allowlist, and says what to do instead. The allowlist is the enforcement; the prompt is
not.

**`correct_fact` addresses by identity key `(entity, predicate, qualifier)`, never by
fact id.** `readtools._edge_line` prints an entity's facts as `predicate: statement` and
prints no fact id at all, so id-addressing would force a `read_entity` v5 and a second
addressing vocabulary the model has to learn on top of the one it already reads. The key
the model can SEE is the key it writes. On a key that holds several live rows — a
set-valued relationship, where each distinct object is a co-equal current edge — the
handler lists what is there and REFUSES: a correction of one of them is not an operation
this graph has, because a set-valued edge's identity is its object. See the comment at
the check for why the `replaces` retry the tool used to offer could not work.

**The four writer-backed handlers share one writer per conversation** (every one but
`merge_entities`, which stages a Proposal and writes no graph). `NoteGraphWriter` owns the call
budgets and the handle table, so building one per call makes both inert — which is what
`CORRECT_CALL_BUDGET` did until it was found reporting "5 calls left" on the seventh
consecutive correction.

**The correction itself is one flag, not a mechanism.** `ExtractedFact.correction` is
the field D11 keeps, and `supersession.decide()` is where it means anything: on a
single-head address it supersedes every current head and commits active + pinned
regardless of temporal order. So `correct_fact` writes through the same `commit_facts`
`assert_fact` does, sets that one field, and reports what `decide()` did. `decide()`
never becomes a model-facing verb (constraint 5).

**`correct_fact` is not `file_correction` under another name, and the two do not merge.**
It addresses ONE identity key resolved against the graph, refuses a key holding several
live rows, and works only inside a note conversation — while the wiki's correction lever
takes PROSE from a Talk thread or a lint card, in a place with no note conversation at
all, and its whole point is to leave a NOTE behind: the citable source
`wiki_citations.chunk_id` (NOT NULL) needs and the corpus rebuild re-derives the graph
from. So `file_correction` keeps minting the note; what it lost with the analyzer is
only the flag, and `graphwritetools.NoteTarget.is_correction` sets it now.

**`merge_entities` can only ever STAGE** (constraint 12). A fold is a full-owner write —
`merge_entity_pair` asks Postgres `app.is_full_owner()` before any statement, because RLS
silently narrows its four `UPDATE`s and a cross-domain fold would half-complete with some
facts repointed and others stranded on the tombstone. A note conversation runs narrowed
to `(note_domain, 'general')` by construction, so it cannot enact one and must not be
given a path that tries. It stages the Proposal the owner's approval enacts — and the
enact runs `SqlAnalysisRepo.merge_entities`, which is already the one fold-and-repoint
the review inbox uses, tombstone check and `distinct_from` check included.

**The direction is server-chosen.** `plan_merge` ranks the pair at ENACT time (a
subject-linked identity outranks a bare one, confirmed outranks provisional, older breaks
the tie), so the owner is never merged away and the model never picks a survivor. The
tool's own prose names both entities and asserts no direction, exactly as `propose_merge`
does — this is the same node op, so the two paths cannot diverge.

Every failure here is TEXT. `loop.py` turns a raised exception into a generic "hit an
internal error" the model learns nothing from, so a refusal that cannot say what to do
instead is a refusal the model will simply repeat.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import ProposalRef
from jbrain.agent.graphwritetools import (
    ASSERT_FACT,
    CLOSE_READING,
    RESOLVE_ENTITY,
    NoteGraphWriter,
    NoteTarget,
)
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.proposals import NodeSpec, ProposalRepo, ProposalSpec
from jbrain.agent.toolregistry import ToolHandler
from jbrain.analysis.entities import are_distinct, live_entity_by_id
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.note_conversation import NoteConversationRepo
from jbrain.schema import get_registry

if TYPE_CHECKING:
    from jbrain.agent.readtools import EntityReader
    from jbrain.analysis.pipeline import AnalysisPipeline
    from jbrain.llm import LlmRouter
    from jbrain.notes.service import NoteInfo, NotesRepo

log = structlog.get_logger()

CORRECT_FACT = "correct_fact"
MERGE_ENTITIES = "merge_entities"

# The on-reply WRITE verbs, named once. `agents.NOTE_INGEST_ON_REPLY_TOOLS` allowlists
# them and `toolregistry.NEVER_DEFAULT` excludes them from curator's `allow=None`
# wildcard; both are asserted in tests, because either alone is not enough (constraint 9).
REPLY_WRITE_TOOLS = frozenset({CORRECT_FACT, MERGE_ENTITIES})

# How many live rows one identity key is listed back with. Not a cap on anything — a
# set-valued predicate ("owns", "attended") legitimately holds many — only the point past
# which the listing stops being useful to read back.
_MAX_HANDLES = 8

# How many conversations' writers this registry keeps alive at once. A writer holds the
# note's chunk text, its handle table and its call budgets, so it must not be immortal;
# a handful covers every thread a person has open, and evicting one only costs the next
# call a re-read.
_MAX_LIVE_WRITERS = 32


@dataclass(frozen=True)
class Named:
    """One entity the model named, as the two tools need it: the entity page the turn
    may read, plus the two ids `entity_view` does not carry — the LIVE id (a merge
    tombstone followed to its survivor) and `subject_id`, which is part of the write
    path's identity key."""

    view: dict[str, Any]
    entity_id: uuid.UUID
    subject_id: uuid.UUID | None

    @property
    def name(self) -> str:
        return str(self.view["canonical_name"])

    @property
    def domain(self) -> str:
        return str(self.view["domain"])


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _as_uuid(token: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(token)
    except (ValueError, AttributeError, TypeError):
        return None


async def _note_for_session(
    maker: async_sessionmaker[AsyncSession], ctx: ToolContext
) -> tuple[str, str] | None:
    """`(note_id, session_id)` for the note conversation this turn belongs to, or None.

    The conversation row is the ONLY source of the note id — never an argument. It is
    read under the turn's own context, so a session the owner cannot see resolves to
    nothing rather than to someone else's note."""
    session_id = ctx.agent_session_id
    if session_id is None:
        return None
    repo = NoteConversationRepo()
    async with scoped_session(maker, ctx.session) as s:
        conversation = await repo.get(s, session_id)
    if conversation is None:
        return None
    return str(conversation.note_id), session_id


def _current_groups(
    view: Mapping[str, Any], predicate: str, qualifier: str
) -> list[dict[str, Any]]:
    """The entity page's LIVE rows at one identity key.

    `entity_view` groups facts by `(predicate, qualifier)` and, for a set-valued
    relationship, additionally by object — which is exactly the multi-row shape this key
    can hold. Predicates are matched through the schema registry's normalization on both
    sides, so a model that writes `blood_pressure` finds the `bloodPressure` the graph
    stored (the same separator- and case-insensitivity `domain_floor` needed)."""
    registry = get_registry()
    want = registry.normalize_predicate(predicate).casefold()
    groups = []
    for group in view.get("predicates", []):
        current = group.get("current")
        if not current:
            continue
        if registry.normalize_predicate(str(group.get("predicate", ""))).casefold() != want:
            continue
        if str(group.get("qualifier") or "") != qualifier:
            continue
        groups.append(current)
    return groups


def _head_line(handle: str, row: Mapping[str, Any]) -> str:
    """One live head, addressable. The VALUE is what distinguishes the rows of a
    multi-row key, so it leads; the statement is what the owner would recognise."""
    value = row.get("object_entity_name") or _value_text(row) or "(no value)"
    return f"{handle}  → {value} — {row.get('statement') or ''}".rstrip(" —")


def _value_text(row: Mapping[str, Any]) -> str:
    value = row.get("value_json")
    if not isinstance(value, Mapping):
        return ""
    body = value.get("value")
    unit = value.get("unit")
    return f"{body} {unit}".strip() if unit else _clean(body)


def build_reply_write_handlers(
    maker: async_sessionmaker[AsyncSession],
    proposals: ProposalRepo,
    entities: EntityReader,
    notes: NotesRepo,
    router: LlmRouter | None = None,
) -> dict[str, ToolHandler]:
    """The FIVE note-graph tools a REPLY turn dispatches, wired for the chat registry.

    `correct_fact` + `merge_entities` (the on-reply writes) and `resolve_entity` +
    `assert_fact` + `close_reading` (the unattended set, which D8 keeps on the reply turn
    because it is the same agent finishing the same note). All five find their
    conversation through `ToolContext.agent_session_id`, never through an argument.

    ⟲ **`close_reading` was missing from this list** (R3's fourth review). R3 bound it and
    R3's fourth round re-wired it below, and it is the one worth naming: the other four
    state ONE thing each, and this one states a WHOLE-NOTE READING — which is what
    `clarify.settle_conversation` retracts against, and so the only verb here whose
    omission changes what the note stops asserting.

    `router` is what an `AnalysisPipeline` needs to exist; none of these use the model
    calls on it (`commit_facts` is deterministic), but the pipeline is the object that
    owns the write path and it does not construct without one. On a box with no router
    the handler refuses in text rather than the sidecar vanishing: an allowlisted tool
    that is silently absent is a tool the model is told it has."""
    pipeline: AnalysisPipeline | None = None
    # One `NoteGraphWriter` per CONVERSATION, for the life of this registry — which is
    # the process's, since `readtools.build_registry` is called once at startup.
    #
    # Not an optimisation. The writer owns the call budgets and the handle table, and
    # building one per call made both inert: `CORRECT_CALL_BUDGET` re-created a fresh
    # `ToolCallBudget(6)` on every call, so seven consecutive corrections each reported
    # "5 calls left" and the seventh still wrote — the exact failure `graphwritetools`
    # says the engine-side budget exists to prevent, because "a prompt-stated cap does
    # not hold". `adopt()` likewise restarted at `e1` every time.
    #
    # Bounded, and small: a thread that has gone quiet must not pin its writer (and the
    # note's chunk text) in memory forever. Evicting one only costs the next call a
    # re-read of the note and a re-mint of its handles — and it CANNOT be used to reset a
    # budget, because eviction is oldest-first and never reachable from inside a turn.
    writers: OrderedDict[str, tuple[frozenset[str], NoteGraphWriter]] = OrderedDict()

    def _pipeline() -> AnalysisPipeline | None:
        nonlocal pipeline
        if pipeline is None and router is not None:
            from jbrain.analysis.pipeline import AnalysisPipeline as _Pipeline

            pipeline = _Pipeline(maker, router)
        return pipeline

    def _writer(
        session_id: str, note: NoteInfo, analyzer: AnalysisPipeline, ctx: ToolContext
    ) -> NoteGraphWriter:
        scopes = frozenset(ctx.scopes)
        existing = writers.get(session_id)
        if existing is not None and existing[0] == scopes:
            writers.move_to_end(session_id)
            return existing[1]
        # A cached writer is keyed on the turn's READ SCOPES as well as its conversation.
        # `Handle.visible` is decided at mint time, so a handle minted while the turn
        # could see `health` would keep reporting that entity's canonical name after the
        # turn narrowed. Unreachable today — a note thread's scopes are recomputed from
        # the note and `set_scopes` refuses an engine-only persona — but the cost of the
        # invariant is one comparison and the cost of it being wrong is a name leaving its
        # domain. The BUDGETS carry across the rebuild: a rebuild must never be a way to
        # buy calls.
        writer = NoteGraphWriter(
            maker,
            analyzer,
            target=NoteTarget(
                note_id=uuid.UUID(note.id),
                domain=note.domain,
                captured_at=note.created_at,
                tz_offset_minutes=note.tz_offset_minutes,
                # Carried on the reply turn for the same reason the unattended pass
                # carries it: a correction note's attested facts are the owner
                # out-arguing the graph whichever turn reads them, and the old path
                # elevated per NOTE, not per pass. `correct_fact` is unaffected — it
                # passes `correction=True` outright.
                provenance=note.provenance,
            ),
            # The owner at FULL scope, exactly as the unattended writes run (constraint
            # 2): resolution layer 1 carries no domain predicate, and a floored fact write
            # is refused by RLS outright on a narrowed session. `read_scopes` stays the
            # TURN's, so a cross-domain entity's canonical name is still withheld from the
            # result text — the write widens, the reporting does not.
            write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
            read_scopes=ctx.scopes,
            extractor="note_ingest_reply",
        )
        if existing is not None:
            writer.resolve_budget = existing[1].resolve_budget
            writer.assert_budget = existing[1].assert_budget
            writer.correct_budget = existing[1].correct_budget
            # The reading carries too, and it carries for a second reason on top of the
            # budget one: `Reading.clamped` says this thread's reading is a PREFIX of the
            # note, and a rebuild that dropped it would launder an incomplete reading into
            # a complete-looking one — which is the exact input the settle's gate is built
            # to refuse (`AGENT_INGEST_REWRITE.md` §2).
            writer.reading_budget = existing[1].reading_budget
            writer.reading = existing[1].reading
        writers[session_id] = (scopes, writer)
        while len(writers) > _MAX_LIVE_WRITERS:
            # ⚠ EVICTION DOES WHAT THE COMMENT ABOVE SAYS MUST NOT HAPPEN, and nothing
            # here fixes it (R3's third review, F5, filed as a residual under R3 in
            # `AGENT_INGEST_REWRITE.md` §7). A rebuild carries the `Reading` by hand; an
            # eviction loses the writer outright, so the thread's next call starts a fresh
            # `Reading()` with `clamped=False` — a clamped prefix presenting as a complete
            # reading. It costs nothing while the reply path settles with `reading=None`
            # (`api/agent.py`): no reading is read back here, so no laundered one can
            # license a retraction. It becomes real the moment R3f or R4 closes that
            # residual, and the fix is a reading that outlives the cache slot rather than
            # a third hand-carry.
            writers.popitem(last=False)
        return writer

    async def _bound(ctx: ToolContext, tool: str) -> tuple[NoteGraphWriter | None, str | None, str]:
        """The writer for this turn's conversation, or the refusal text to return.

        The whole gate the four WRITER-BACKED verbs share — `correct_fact`,
        `resolve_entity`, `assert_fact` and `close_reading`, named rather than counted
        (R3's fourth review found this reading as "the four" of the docstring above, which
        is a different set): a note conversation this principal can see, a note that still
        exists, and a write path on this box. `merge_entities` is the fifth tool here and
        does not come through it — it stages a Proposal and needs no writer."""
        found = await _note_for_session(maker, ctx)
        if found is None:
            return (
                None,
                None,
                (
                    f"{tool} works only inside a note's conversation, and this turn is not"
                    " one. Nothing was changed."
                ),
            )
        note_id, session_id = found
        analyzer = _pipeline()
        if analyzer is None:
            return (
                None,
                None,
                (
                    f"{tool} cannot reach the write path on this box. Nothing was changed —"
                    " tell Jeff it was not recorded."
                ),
            )
        note: NoteInfo | None = await notes.get_note(ctx.session, note_id)
        if note is None:
            return (
                None,
                None,
                (
                    "the note this conversation is about is gone, so there is nothing to"
                    " record against."
                ),
            )
        return _writer(session_id, note, analyzer, ctx), note_id, ""

    async def _resolve_one(ctx: ToolContext, token: str, *, field: str) -> tuple[Named | None, str]:
        """One entity the model named, under the TURN's read scopes.

        An id or a name, both resolved through the RLS-scoped reads the persona already
        holds — so the only entities reachable here are the ones `find_entity` /
        `read_entity` could have shown it. That is the domain gate on both tools: the
        write session `correct_fact` opens runs at full owner scope (constraint 2), but
        nothing it can be POINTED at was ever outside the conversation's own scope.

        The id is then followed through `live_entity_by_id` to the survivor, so an id the
        owner has since merged away resolves to the entity it became rather than to a
        tombstone — correcting a fact onto a tombstone, or folding onto one, resurrects
        the duplicate an earlier merge removed. That call is also where `subject_id`
        comes from, which `entity_view` does not carry and the write path's identity key
        needs: without it a correction to one of the owner's own facts would filter on
        `subject_id IS NULL`, miss the head it meant to supersede, and land beside it."""
        if not token:
            return None, f"{field} is empty. Name the entity, or give the id find_entity showed."
        eid = _as_uuid(token)
        if eid is None:
            rows = [
                row
                for row in await entities.list_entities(ctx.session, q=token, limit=12)
                if _clean(row.get("canonical_name")).casefold() == token.casefold()
                or token.casefold() in {_clean(a).casefold() for a in (row.get("aliases") or [])}
            ]
            if not rows:
                return None, (
                    f'nothing in scope is called "{token}". Use find_entity to get the'
                    " right one, then pass its id."
                )
            if len(rows) > 1:
                names = ", ".join(f"{r['canonical_name']} (id={r['id']})" for r in rows[:4])
                return None, (
                    f'several entities answer to "{token}": {names}. Pass the id of the'
                    " one you mean."
                )
            eid = _as_uuid(str(rows[0]["id"]))
        async with scoped_session(maker, ctx.session) as s:
            live = None if eid is None else await live_entity_by_id(s, eid)
        if live is None:
            return None, (
                f'"{token}" is not an entity this conversation can see, or it has been'
                " folded away with nowhere to go. Use find_entity."
            )
        view = await entities.entity_view(ctx.session, str(live.id))
        if view is None:
            return None, f'"{token}" is not an entity this conversation can see.'
        named = Named(
            view=dict(view), entity_id=uuid.UUID(str(live.id)), subject_id=live.subject_id
        )
        return named, ""

    async def correct_fact_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        writer, note_id, refusal = await _bound(ctx, CORRECT_FACT)
        if writer is None or note_id is None:
            return refusal

        named, why = await _resolve_one(ctx, _clean(arguments.get("entity")), field="entity")
        if named is None:
            return f"correct_fact: {why}"
        predicate = _clean(arguments.get("predicate"))
        if not predicate:
            return (
                "correct_fact needs a `predicate` — the relation whose value is wrong,"
                " spelled as read_entity shows it."
            )
        registry = get_registry()
        predicate, qualifier = registry.decompose_predicate(
            predicate, _clean(arguments.get("qualifier"))
        )
        value = _clean(arguments.get("object"))
        if not value:
            return (
                "correct_fact needs an `object` — the value that is actually right. It"
                " replaces what is on file; it does not describe what is wrong with it."
            )

        # The identity key resolved against the graph BEFORE anything is written. A key
        # holding several live rows does not name one fact, and — this is the part that
        # matters — there is nothing `correct_fact` can honestly do about it.
        #
        # `entity_view` yields several groups at one (predicate, qualifier) ONLY for a
        # non-functional relationship, where it splits per object (`analysis/repo.py`),
        # while `decide()`'s correction branch acts only on a `single_head` address —
        # state, attribute, preference, or a FUNCTIONAL relationship. The two conditions
        # are exclusive, so on every key that can hold several rows the `correction` flag
        # is a no-op and the write falls through to the ordinary accumulate path.
        #
        # A set-valued edge's identity IS its object (`pipeline._facts_at_key` keeps
        # `object_entity_id` in the key for a non-functional predicate), so "replace the
        # Civic edge with a bicycle" is not one fact changing value — it is one fact
        # ending and another beginning, which this data model has no single write for.
        # The handler used to mint f1/f2 handles and invite a retry naming one; that
        # retry left both original edges live, added a third, and reported `ok … replaced`
        # — the worst of the three outcomes. So the listing stays (the model still has to
        # be able to tell the owner what IS on file) and the affordance that could not
        # work is gone.
        heads = _current_groups(named.view, predicate, qualifier)
        # THE EMPTY-ADDRESS ARM, refused on a turn whose words the note never received
        # (R3's second review, finding 3). `decide()`'s correction branch commits active
        # + PINNED, so at an address holding nothing this verb does not correct anything —
        # it MINTS a new fact that no later note can supersede. On a turn where the
        # owner's words became the note's text that is the designed behaviour and stays.
        # On a turn where they did not, it is the worst write in the system: the row cites
        # text that exists nowhere, so no re-reading of the note can ever falsify it, and
        # no correction note can reach it either. `narrow_for_unprompted_reply` takes
        # `assert_fact` off exactly that turn, and leaving this arm bound made the refusal
        # a lie — the agent told it cannot record a new fact, holding a verb that records
        # one permanently.
        #
        # The condition is read off `ctx.agent_tools`, this turn's effective allowlist,
        # and that is not a proxy here: `assert_fact` is absent from it for exactly two
        # reasons, and the other one (the unattended pass, where it left the set in R3)
        # binds no `correct_fact` handler at all — this code is on the CHAT registry, so
        # reaching this line at all means a reply turn. The EMR and third-party narrowings
        # take both verbs together, so they cannot land here either. What is left is the
        # narrowing above, which is the invariant: the owner's words did not land on the
        # note as source text.
        #
        # Correcting an EXISTING head is untouched — that is what the verb is for, and
        # pinning is the designed mechanism for it.
        if not heads and ASSERT_FACT not in ctx.agent_tools:
            return (
                f"{named.name}.{predicate} holds nothing on file, so this would not"
                " correct a fact — it would record a NEW one, pinned, that no later note"
                " could ever change. Jeff's words on this turn did not reach the note, so"
                " there is no text behind it: nothing was recorded. Tell him it is not"
                " recorded, and that a note of his own — or an answer to a question you"
                " ask him now — is how it lands. You can still correct anything that IS"
                " on file."
            )
        if len(heads) > 1:
            listing = "\n".join(
                _head_line(f"f{i + 1}", row) for i, row in enumerate(heads[:_MAX_HANDLES])
            )
            return (
                f"{named.name}.{predicate} holds {len(heads)} values at once, and they"
                " are co-equal — each is its own fact, not a version of one value:\n"
                f"{listing}\n"
                "Nothing was changed, and correct_fact cannot single one out. Tell Jeff"
                " what is on file and ask which of these he means."
            )

        # An `object` that is an entity id is resolved HERE, under the turn's own read
        # scopes, and adopted as a handle — the write path addresses entities by handle
        # and nothing else. Passed through raw it resolved against a handle table holding
        # only the subject, so it never matched: the row landed with
        # `object_entity_id = NULL`, the bare uuid as its literal value and `pinned=True`,
        # the real edge superseded, and "ok … replaced" reported back. `read_entity` and
        # the wiki then rendered "Jeff works for f458b192-…", and because it was pinned
        # nothing could auto-correct it.
        object_token = value
        if _as_uuid(value) is not None:
            target, why = await _resolve_one(ctx, value, field="object")
            if target is None:
                return f"correct_fact: {why}"
            object_token = writer.adopt(
                entity_id=target.entity_id,
                subject_id=target.subject_id,
                surface=target.name,
                name=target.name,
                kind=str(target.view.get("kind") or "Thing"),
                domain=target.domain,
            ).handle

        subject = writer.adopt(
            entity_id=named.entity_id,
            subject_id=named.subject_id,
            surface=named.name,
            name=named.name,
            kind=str(named.view.get("kind") or "Thing"),
            domain=named.domain,
        )
        out = await writer.correct_fact(
            {
                "subject": subject.handle,
                "predicate": predicate,
                "qualifier": qualifier,
                "object": object_token,
                "statement": _clean(arguments.get("statement")),
                "when": _clean(arguments.get("when")),
            }
        )
        log.info(
            "correct_fact.recorded",
            note_id=note_id,
            entity_id=str(named.entity_id),
            predicate=predicate,
        )
        return out

    async def merge_entities_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        if await _note_for_session(maker, ctx) is None:
            return (
                "merge_entities works only inside a note's conversation, and this turn"
                " is not one. Nothing was staged."
            )
        if not ctx.session.principal_id:
            return "merge_entities can't stage a fold without an owner principal."
        a, why = await _resolve_one(ctx, _clean(arguments.get("entity_a")), field="entity_a")
        if a is None:
            return f"merge_entities: {why}"
        b, why = await _resolve_one(ctx, _clean(arguments.get("entity_b")), field="entity_b")
        if b is None:
            return f"merge_entities: {why}"

        # Both ids arrive already followed to their live rows (`_resolve_one`), so a pair
        # that has ALREADY been folded reads as one entity here rather than staging a
        # second fold onto a tombstone — the shape `analysis/repo.resolve_review`'s
        # merge-accept arm still reaches, and the reason its shape is not copied.
        if a.entity_id == b.entity_id:
            return f"“{a.name}” and “{b.name}” are already the same entity — nothing to merge."
        async with scoped_session(maker, ctx.session) as s:
            # A rejected merge writes a permanent `distinct_from`, and the enact refuses
            # on it. Checking here means the owner is not handed a card whose only
            # possible outcome is a refusal of a question he already answered.
            if await are_distinct(s, a.entity_id, b.entity_id):
                return (
                    f"Jeff has already said “{a.name}” and “{b.name}” are different"
                    " people or things, so they cannot be merged. Nothing was staged."
                )

        domain = a.domain
        # A fold spans domains by construction (an entity's facts carry their own
        # domain), and constraint 12 makes enacting one a full-owner write. A note
        # conversation is narrowed to `(note_domain, 'general')`, so it may not even
        # stage a fold into a domain it cannot read: the card it would raise names two
        # entities it cannot check, for a write it cannot make.
        if ctx.scopes and (domain not in ctx.scopes or b.domain not in ctx.scopes):
            return (
                f"this conversation isn't scoped to '{domain}', so it can't stage a fold"
                " there. Nothing was staged."
            )
        name_a, name_b = a.name, b.name
        reason = _clean(arguments.get("reason"))
        # The SAME node op the chat persona's `propose_merge` stages, so the owner's
        # approval runs the one enact path (`entity_merge_executor` ->
        # `SqlAnalysisRepo.merge_entities`) and the two can never diverge. The ids ride
        # structurally in the preview, never in the prose, and no direction is asserted:
        # `plan_merge` ranks the pair at enact.
        node = NodeSpec(
            id=str(uuid.uuid4()),
            type="leaf",
            op="merge_entities",
            label=f"Merge “{name_a}” and “{name_b}”",
            preview={
                "entity_a": str(a.entity_id),
                "entity_b": str(b.entity_id),
                "name_a": name_a,
                "name_b": name_b,
                "kind_a": str(a.view.get("kind") or ""),
                "kind_b": str(b.view.get("kind") or ""),
                "domain": domain,
                **({"reason": reason} if reason else {}),
            },
        )
        prop_id = await proposals.stage(
            ctx.session,
            principal_id=ctx.session.principal_id,
            spec=ProposalSpec(
                kind="merge",
                domain=domain,
                title=f"Merge “{name_a}” and “{name_b}”",
                nodes=[node],
                # The notes tab of the review inbox unions the Proposals a note
                # conversation raised, so the session id is what makes this card land
                # beside its own thread rather than in the general inbox.
                provenance={"source": "note_conversation"},
                session_id=ctx.agent_session_id,
            ),
        )
        log.info(
            "merge_entities.staged",
            proposal_id=prop_id,
            a=str(a.entity_id),
            b=str(b.entity_id),
        )
        return ToolOutput(
            f"Staged a fold of “{name_a}” and “{name_b}” for Jeff to approve. Folding two"
            " entities is a write this conversation is not allowed to make on its own, so"
            " nothing has changed yet — when he approves it, the more-anchored identity"
            " survives and the other's mentions and facts repoint onto it, so nothing is"
            " lost. Say that it is waiting on him; do not say they are merged.",
            proposal=ProposalRef(proposal_id=prop_id, kind="merge"),
        )

    def _texted(name: str, handler: ToolHandler) -> ToolHandler:
        async def run(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
            try:
                return await handler(arguments, ctx)
            except Exception as exc:  # noqa: BLE001 — a refusal the model can read
                log.warning("replytools.failed", tool=name, error=repr(exc))
                return (
                    f"{name} failed and nothing was changed. Tell Jeff it did not go"
                    " through rather than describing it as done."
                )

        return run

    async def resolve_entity_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        writer, _note_id, refusal = await _bound(ctx, RESOLVE_ENTITY)
        if writer is None:
            return refusal
        return await writer.resolve_entity(arguments, ctx)

    async def assert_fact_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        writer, _note_id, refusal = await _bound(ctx, ASSERT_FACT)
        if writer is None:
            return refusal
        return await writer.assert_fact(arguments, ctx)

    async def close_reading_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        writer, _note_id, refusal = await _bound(ctx, CLOSE_READING)
        if writer is None:
            return refusal
        # THE CORRECTION-NOTE ELEVATION IS OFF on a turn whose words the note never
        # received (R3's third review, finding 1), on the same signal and for the same
        # reason as `correct_fact`'s empty-address arm above. `_assert_one` elevates an
        # ATTESTED element of an `owner_correction` note to `correction=True`, which
        # force-supersedes and PINS — and `_attests` only checks that the quote string is
        # in the note, never that it supports the object, so a reply turn could pair a
        # real line of the note with a value the owner had only typed in chat and mint a
        # row at confidence 1.0 that no sweep, no later note and no correction note can
        # ever reach. Reaching THIS line means the chat registry, so it is a reply turn,
        # and `assert_fact` is absent from its allowlist for one reason:
        # `narrow_for_unprompted_reply` took it off. The EMR and third-party narrowings
        # cannot present here — the first takes `close_reading` too (so dispatch never
        # arrives), and the second only applies to a note a STRANGER wrote, which is never
        # an `owner_correction` one, so the elevation is unreachable on it either way.
        #
        # The unpinned row such an element still commits is O16's open loss shape, left
        # open deliberately: it is falsifiable by the next reading and swept when one
        # comes. What is closed here is the PERMANENT shape.
        return await writer.close_reading(
            arguments, ctx, words_reached_note=ASSERT_FACT in ctx.agent_tools
        )

    return {
        CORRECT_FACT: _texted(CORRECT_FACT, correct_fact_tool),
        MERGE_ENTITIES: _texted(MERGE_ENTITIES, merge_entities_tool),
        # The unattended pair, on the reply turn. `NOTE_INGEST_ON_REPLY_TOOLS` has always
        # allowlisted both — D8's set is a superset, so that taking `assert_fact` away
        # "at the moment the owner explains what the note actually meant would leave it
        # able to discuss a correction and unable to record one" — but the chat registry
        # dropped the sidecars unconditionally, so neither was ever offered and neither
        # could dispatch. The consequence was not a missing feature: the reply turn's only
        # remaining write verb was `correct_fact`, whose empty-address path commits
        # `insert_pinned=True`, so EVERY fact the owner taught the thread was pinned
        # against future supersession — including by later notes.
        #
        # They bind here for the same reason `ask_owner` does, and the reason the drop was
        # ever justified does not survive it: a handler is bound to one note, but the note
        # comes from the CONVERSATION ROW (`_note_for_session`), never from an argument,
        # so there is exactly one note a given turn can write and a chat turn outside a
        # note conversation reaches none. The two locks that were doing the real work are
        # untouched — `NOTE_INGEST_*_TOOLS` is the allowlist and both names are in
        # `toolregistry.NEVER_DEFAULT`, so curator's wildcard cannot absorb them.
        RESOLVE_ENTITY: _texted(RESOLVE_ENTITY, resolve_entity_tool),
        ASSERT_FACT: _texted(ASSERT_FACT, assert_fact_tool),
        # And the reading, for the same reason and by the same route. The on-reply set is
        # a SUPERSET of the unattended one, so a reply turn that has re-read the whole
        # note must be able to state it — a turn that can only add facts one at a time
        # can never say what the note says NOW, which is the claim the settle needs
        # (`AGENT_INGEST_REWRITE.md` §1). Its budget and its `Reading` are shared with
        # every other reply turn on this thread — one writer per conversation, carried
        # across a scope-change rebuild above — but NOT with the unattended pass, which
        # ran in the worker: two processes, no shared memory, so a reply turn starts with
        # an empty reading exactly as it starts with an empty handle table. That is why
        # the settle's gate reads the pass's own writer at the pass's own terminal block
        # rather than expecting the flag to survive the trip.
        CLOSE_READING: _texted(CLOSE_READING, close_reading_tool),
    }
