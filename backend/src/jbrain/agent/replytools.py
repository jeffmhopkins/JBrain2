"""`correct_fact` and `merge_entities` — the two write verbs that need Jeff in the room.

W3 of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, built to
docs/research/agent-ingest/TOOL_SURFACE.md's on-reply rows. D8 splits the note persona's
surface in two and `agents.agent_for_owner_reply` is the seam; these are the handlers
behind the half that only a turn the owner sent can reach.

**Both are bound on the CHAT registry, not on the worker's per-note one.** The owner's
reply into a note thread is an ordinary `/chat` turn, so that is the only registry the
reply turn consults — the same reason `ask_owner` is wired there. Neither takes a note
id: both find their conversation through `ToolContext.agent_session_id`, and outside a
note conversation both refuse. A write primitive a hostile body could point at another
note is not a tool, it is a hole.

**`correct_fact` addresses by identity key `(entity, predicate, qualifier)`, never by
fact id.** `readtools._edge_line` prints an entity's facts as `predicate: statement` and
prints no fact id at all, so id-addressing would force a `read_entity` v5 and a second
addressing vocabulary the model has to learn on top of the one it already reads. The key
the model can SEE is the key it writes. On a key that holds several live rows — a
set-valued relationship, where each distinct object is a co-equal current edge — the
handler mints `f1`/`f2` handles, writes nothing, and the model retries naming which one.
The handles are positional over the entity page's own grouping, so they are stable for
the retry without any per-conversation state to keep.

**The correction itself is one flag, not a mechanism.** `ExtractedFact.correction` is
what survives from the retired correction-note path (D11), and `supersession.decide()`
is where it means anything: on a single-head address it supersedes every current head
and commits active + pinned regardless of temporal order. So `correct_fact` writes
through the same `commit_facts` `assert_fact` does, sets that one field, and reports what
`decide()` did. `decide()` never becomes a model-facing verb (constraint 5).

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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import ProposalRef
from jbrain.agent.graphwritetools import NoteGraphWriter, NoteTarget
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

# How many live rows one identity key may hold before the model has to say which. Not a
# cap on anything — a set-valued predicate ("owns", "attended") legitimately holds many —
# only the point past which the listing stops being useful to read back.
_MAX_HANDLES = 8


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
    """`correct_fact` + `merge_entities`, wired for the chat registry.

    `router` is what an `AnalysisPipeline` needs to exist; `correct_fact` uses none of
    the model calls on it (`commit_facts` is deterministic), but the pipeline is the
    object that owns the write path and it does not construct without one. On a box with
    no router the handler refuses in text rather than the sidecar vanishing: an
    allowlisted tool that is silently absent is a tool the model is told it has."""
    pipeline: AnalysisPipeline | None = None

    def _pipeline() -> AnalysisPipeline | None:
        nonlocal pipeline
        if pipeline is None and router is not None:
            from jbrain.analysis.pipeline import AnalysisPipeline as _Pipeline

            pipeline = _Pipeline(maker, router)
        return pipeline

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
        found = await _note_for_session(maker, ctx)
        if found is None:
            return (
                "correct_fact works only inside a note's conversation, and this turn is"
                " not one. Nothing was changed."
            )
        note_id, _session_id = found
        analyzer = _pipeline()
        if analyzer is None:
            return (
                "correct_fact cannot reach the write path on this box. Nothing was"
                " changed — tell Jeff the correction was not recorded."
            )
        note: NoteInfo | None = await notes.get_note(ctx.session, note_id)
        if note is None:
            return (
                "the note this conversation is about is gone, so there is nothing to"
                " record a correction against."
            )

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
        # holding several live rows (a set-valued relationship: each distinct object is a
        # co-equal current edge) does not name one fact, so the handler mints handles over
        # the entity page's own grouping and writes nothing. The handles are positional
        # rather than stored, so the retry re-derives them from the same page — and if the
        # graph moved underneath, it re-derives them from the page as it now stands, which
        # is the only listing worth acting on anyway.
        heads = _current_groups(named.view, predicate, qualifier)
        replaces = _clean(arguments.get("replaces"))
        if len(heads) > 1 and not replaces:
            listing = "\n".join(
                _head_line(f"f{i + 1}", row) for i, row in enumerate(heads[:_MAX_HANDLES])
            )
            return (
                f"{named.name}.{predicate} holds {len(heads)} live values at once, so"
                " that address does not name one fact:\n"
                f"{listing}\n"
                "Nothing was changed. Call correct_fact again with `replaces` set to the"
                " handle of the one that is wrong."
            )
        if replaces and heads:
            index = replaces.lower().removeprefix("f")
            if not index.isdigit() or not 1 <= int(index) <= len(heads):
                return (
                    f"correct_fact: no such handle {replaces!r} at"
                    f" {named.name}.{predicate}. Call it again without `replaces` to see"
                    " the handles as they stand now."
                )

        writer = NoteGraphWriter(
            maker,
            analyzer,
            target=NoteTarget(
                note_id=uuid.UUID(note_id),
                domain=note.domain,
                captured_at=note.created_at,
                tz_offset_minutes=note.tz_offset_minutes,
            ),
            # The owner at FULL scope, exactly as the unattended writes run (constraint
            # 2): resolution layer 1 carries no domain predicate, and a floored fact write
            # is refused by RLS outright on a narrowed session. `read_scopes` stays the
            # TURN's, so a cross-domain entity's canonical name is still withheld from the
            # result text — the write widens, the reporting does not.
            write_ctx=SessionContext(principal_id="worker", principal_kind="owner"),
            read_scopes=ctx.scopes,
            extractor="note_ingest_correction",
        )
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
                "object": value,
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

    return {
        CORRECT_FACT: _texted(CORRECT_FACT, correct_fact_tool),
        MERGE_ENTITIES: _texted(MERGE_ENTITIES, merge_entities_tool),
    }
