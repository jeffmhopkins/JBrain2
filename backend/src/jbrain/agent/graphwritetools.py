"""`resolve_entity` and `assert_fact` — the two tools that write the graph.

W3/T2a of docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, built to the ratified design in
docs/research/agent-ingest/TOOL_SURFACE.md. These are the note-ingestion persona's
UNATTENDED writes (D8): the agent reads a note as turn 0, resolves the surfaces it
names, and asserts what the note says — while the owner is asleep and nothing
outward-facing is bound.

**The model supplies meaning; the engine supplies mechanics.** Nothing here decides how
a write lands. `resolve_entity` runs the shipped layered resolver; `assert_fact` builds
an `Extraction` and hands it to W1's `commit_facts`, where `supersession.decide()`, the
domain floor, the ratchet, span attestation and the projections do exactly what they do
for the whole-note pipeline. There is no second write path (plan constraint 5:
`decide()` stays the implementation of the write tool, never a model-facing verb).

Five properties that are load-bearing rather than incidental:

- **The batch is the shape, and it was measured, not guessed.** `evals/shape_probe.py`
  put both schemas in front of the live model through `/api/debug/tool-probe`: batched
  arrays-of-objects came back 20/20 well-formed, at 7.6 facts and 8.9 entities per turn,
  against exactly 1.0 for the flat one-per-call fallback. On a serial GPU with the owner
  waiting (plan risk 4) that is the difference between one round trip and eight.
- **Every field the write needs is REQUIRED** (TOOL_SURFACE R3). Across 85 consecutive
  `scratch_write` calls gpt-oss filled the required field every time and the optional one
  never once, and llama.cpp compiles `required` into the tool grammar. `quote` is
  required even though inferred facts commit (D2): the model cannot CLAIM attestation,
  it can only offer text, and this module checks the text against the note.
- **No `domain` field, ever** (the firewall red-team rule, `arbiter.py:163-165`). The
  agent's D18 choice of domain for a novel predicate is expressed by the PREDICATE it
  writes, resolved deterministically here: the spelling is normalized through the schema
  registry and then `domain_floor` decides, so a floored predicate cannot be dodged by
  spelling it `blood_pressure` instead of `bloodPressure`. Everything else takes the
  note's domain and can only ratchet UP.
- **A batch never rolls back its successful elements.** Each element commits inside its
  own SAVEPOINT, so element 4 failing cannot undo 1-3 — otherwise whole-note atomicity
  returns through the side door.
- **Failures are TEXT, never a raise.** `loop.py:_dispatch` turns an exception into a
  generic "hit an internal error" the model learns nothing from, so every refusal here
  is a result line naming what to do instead.

**Handles are per-conversation.** `resolve_entity` hands back `e1`, `e2`, … and
`assert_fact` addresses entities by those (or by the exact surface that earned one).
They live in this writer, which is built once per conversation, because minting is
`resolve_entity`'s alone: accepting an unknown name in `assert_fact` would make it a
second, silent minting path. An unknown handle is a result line telling the model to
resolve first — the error TOOL_SURFACE specifies.

**The write session is the owner's FULL scope, not the conversation's read scope**
(plan constraint 2). `_exact_matches` carries no domain predicate and is layer 1 of
resolution, so resolving under the narrowed `(note_domain, 'general')` scope would
silently mint duplicates of entities the owner already has in health or finance, and
a floored fact write would be refused by RLS outright. The narrowing that stays is the
one that matters: what this module REPORTS BACK. A resolved entity outside the
conversation's scopes is confirmed as a handle — the model needs that to avoid a
duplicate — but its canonical name is withheld, so the note's own words are all the
conversation ever learns about a cross-domain row.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import EntityRef, FactWriteRef
from jbrain.agent.loop import ToolCallBudget, ToolContext, ToolOutput
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolHandler, ToolRegistry
from jbrain.analysis.entities import ResolvedEntity
from jbrain.analysis.extraction import (
    ExtractedFact,
    ExtractedMention,
    ExtractedTemporal,
    Extraction,
    parse_datetime,
)
from jbrain.analysis.pipeline import (
    ALREADY,
    CLOSED,
    HELD,
    HISTORICAL,
    PROMOTED,
    REPLACED,
    AnalysisPipeline,
    FactWrite,
    _ChunkRef,
    local_anchor,
)
from jbrain.analysis.weight import ConfidenceSignals, effective_weight
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.chunker import PARAGRAPH
from jbrain.models.analysis import Entity
from jbrain.models.notes import Chunk
from jbrain.schema import get_registry

log = structlog.get_logger()

RESOLVE_ENTITY = "resolve_entity"
ASSERT_FACT = "assert_fact"

# The two write tools, named once. `agents.NOTE_INGEST_TOOLS` allowlists them and
# `toolregistry.NEVER_DEFAULT` excludes them from the curator's `allow=None` wildcard —
# both are asserted in tests, because either alone is not enough (plan constraint 9).
GRAPH_WRITE_TOOLS = frozenset({RESOLVE_ENTITY, ASSERT_FACT})

# Batch ceilings, from the measured shapes. `maxItems` may not survive llama.cpp's own
# grammar build (it composes nothing with the json_schema validator), so the handler
# clamps and SAYS SO rather than trusting the schema (TOOL_SURFACE, registry note 3).
MAX_ENTITIES = 12
MAX_FACTS = 8

# Per-CONVERSATION call ceilings, engine-side because a prompt-stated cap does not hold
# (the deep-research scout's in-repo lesson). Per conversation rather than per turn: the
# owner's reply opens a second run on the same thread, and a note that has already had
# 8 batched resolve calls does not need another 8 to answer a correction. Generous
# against the measured batch sizes — 8 x 12 surfaces and 10 x 8 facts is far more than
# any note carries — so hitting one is a runaway, not ordinary work.
RESOLVE_CALL_BUDGET = 8
ASSERT_CALL_BUDGET = 10
# `correct_fact` is un-batched (one disputed value, one identity key), so its budget is
# a count of DISAGREEMENTS, not of round trips. Six is more than any one reply carries;
# past it the model is arguing with the graph rather than recording what Jeff said.
CORRECT_CALL_BUDGET = 6

# The kinds a surface may be declared as, mapped onto the resolver's `kind_hint`
# vocabulary. In the DESCRIPTION, never a JSON-Schema `enum`: an enum in a sidecar
# segfaults gpt-oss's harmony grammar (plan constraint 8), so the closed set is
# validated HERE and an unrecognised word degrades to the resolver's own default
# rather than failing the element.
_KIND_HINTS: dict[str, str] = {
    "person": "Person",
    "people": "Person",
    "organization": "Organization",
    "organisation": "Organization",
    "org": "Organization",
    "company": "Organization",
    "place": "Place",
    "location": "Place",
    "event": "Event",
    "appointment": "Event",
    "condition": "MedicalCondition",
    "medication": "Drug",
    "drug": "Drug",
    "animal": "Animal",
    "pet": "Animal",
    "thing": "Thing",
}
_DEFAULT_KIND = "Thing"

# A literal value shaped like a measurement ("178 lb", "5.4 %", "120/80 mmHg" is left to
# the statement). Recognised so the stored `value_json` is `{value, unit}` — the shape
# `supersession.values_equal` can compare ACROSS units, which is what makes a re-read of
# the same reading land as "already recorded" instead of a fact_conflict.
_QUANTITY = re.compile(r"^(-?\d+(?:\.\d+)?)\s*([^\d\s][^\s]{0,15})$")

# Whitespace-insensitive, case-insensitive containment — the same normalization the
# arbiter's span check uses, so "attested" means here what it means there.
_WS = re.compile(r"\s+")

# The confidence signals a tool-written fact carries. A quote the note genuinely
# contains is surface-attested and commits at full weight; anything else is treated the
# way the arbiter treats a fact it has no signals for (`arbiter._CONSERVATIVE`) — the
# 0.4 inferred-overwrite ceiling, which the supersession low-confidence guard then
# refuses to let overwrite a confident prior. That is the whole enforcement: the fact
# still COMMITS (D2, Lever A), it just cannot silently rewrite history on a quote the
# note does not contain.
_ATTESTED = ConfidenceSignals(surface_attested=True, is_supersede=False)
_UNATTESTED = ConfidenceSignals(surface_attested=False, is_supersede=True)

# What the write path did, rendered for the model. This is the model's ONLY window into
# `decide()`, so each line names what the server did UNASKED (TOOL_SURFACE, "Result
# shapes are the ACI").
_OUTCOME_WORDS: dict[str, str] = {
    ALREADY: "already recorded; refreshed",
    CLOSED: "closed the open interval",
    HISTORICAL: "recorded as history — a newer value is already on file",
    PROMOTED: "recorded (it had been held; now live)",
}


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().casefold()


def _quantity_value(literal: str) -> dict[str, Any]:
    """`value_json` for a literal object. A recognised quantity keeps its number and
    unit apart so cross-unit equality works; anything else is stored verbatim under
    `value`, the one key every renderer already understands (`display._structured_label`)
    and the shape `_shape_check` validates against the predicate's declared shape."""
    match = _QUANTITY.match(literal.strip())
    if match is None:
        return {"value": literal}
    number, unit = match.groups()
    return {"value": float(number) if "." in number else int(number), "unit": unit}


def _precision(when: str) -> str:
    """Temporal precision from the ISO shape (TOOL_SURFACE gap 4: precision is derived,
    never a model field). `2026` is a year, `2026-03` a month, `2026-03-14` a day,
    anything longer an instant."""
    body = when.strip()
    if len(body) == 4:
        return "year"
    if len(body) == 7:
        return "month"
    if len(body) == 10:
        return "day"
    return "instant"


def _temporal(when: str, anchor: datetime, tz_offset_minutes: int | None) -> ExtractedTemporal:
    """One ISO string as the temporal the write path stores, or a raise for a value that
    is not a date at all (the caller turns that into a result line and commits the fact
    undated — a fact is not worth losing over its date).

    A bare year or month is expanded to its first day, because `fromisoformat` rejects
    both. A CLOCK time with no offset is read in the note's own zone, never UTC: a 5pm
    local appointment pinned to 17:00Z is a fact that is wrong by the owner's offset
    forever (the same rule `extraction._naive_tz` states for the note.extract path)."""
    body = when.strip()
    precision = _precision(body)
    if precision == "year":
        body = f"{body}-01-01"
    elif precision == "month":
        body = f"{body}-01"
    naive_tz: Any = UTC
    if precision == "instant" and tz_offset_minutes is not None:
        naive_tz = timezone(timedelta(minutes=tz_offset_minutes))
    parsed = parse_datetime(body, naive_tz=naive_tz)
    if parsed is None:
        raise ValueError(when)
    # A date the model resolved against the note is still a date; `anchor` is carried so
    # the caller's normalizers (future -> expected, past -> closed) have the note's local
    # capture instant to compare against. Nothing is inferred from it here.
    del anchor
    return ExtractedTemporal(
        phrase=None, resolved_start=parsed, resolved_end=None, precision=precision
    )


@dataclass(frozen=True)
class NoteTarget:
    """The note a conversation's writes land on. Fixed for the life of the
    conversation: the persona is opened FOR one note (`converse.py`), and a tool that
    took a note id from the model would be a write primitive pointed by untrusted text."""

    note_id: uuid.UUID
    domain: str
    captured_at: datetime
    tz_offset_minutes: int | None = None

    @property
    def anchor(self) -> datetime:
        """The capture instant in the note's LOCAL time — what "this morning" in a note
        resolves against (`pipeline.local_anchor`)."""
        return local_anchor(self.captured_at, self.tz_offset_minutes)


@dataclass
class Handle:
    """One surface the conversation has resolved, and the run handle standing for it."""

    handle: str
    entity: ResolvedEntity
    surface: str
    kind: str
    name: str
    domain: str
    visible: bool
    """Whether the entity's own domain is inside the conversation's read scopes. False
    withholds its canonical name from the result text — the write still lands."""

    @property
    def label(self) -> str:
        """How the model is told to think of this entity: its canonical name when the
        conversation may see the entity's domain, otherwise the surface the note used."""
        return self.name if self.visible else self.surface


class NoteGraphWriter:
    """The graph-write tools for ONE note conversation.

    Built per conversation (not per turn) because the handle table and the call budgets
    are properties of the conversation: the owner's reply is a second run on the same
    thread and must be able to say "no, e3 is the other Dana" without re-resolving from
    scratch."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        pipeline: AnalysisPipeline,
        *,
        target: NoteTarget,
        write_ctx: SessionContext,
        read_scopes: Sequence[str] = (),
        extractor: str = "note_ingest",
    ):
        self._maker = maker
        self._pipeline = pipeline
        self._target = target
        self._write_ctx = write_ctx
        self._read_scopes = frozenset(read_scopes)
        self._extractor = extractor
        self._by_handle: dict[str, Handle] = {}
        self._by_surface: dict[str, Handle] = {}
        self._chunks: list[_ChunkRef] | None = None
        self._note_text = ""
        self.resolve_budget = ToolCallBudget(RESOLVE_CALL_BUDGET)
        self.assert_budget = ToolCallBudget(ASSERT_CALL_BUDGET)
        self.correct_budget = ToolCallBudget(CORRECT_CALL_BUDGET)

    # --- handles ---------------------------------------------------------------

    def _remember(self, handle: Handle) -> None:
        self._by_handle[handle.handle] = handle
        self._by_surface[_norm(handle.surface)] = handle
        # The canonical name is an address too: the model reads it in a result line and
        # will use it as the subject on the next call.
        if handle.visible:
            self._by_surface.setdefault(_norm(handle.name), handle)

    def lookup(self, token: str) -> Handle | None:
        """A handle, or the exact surface that earned one. Nothing else resolves — an
        unknown name here would make `assert_fact` a second minting path, and minting is
        `resolve_entity`'s alone (TOOL_SURFACE: "the only minting path")."""
        key = token.strip()
        return self._by_handle.get(key) or self._by_surface.get(_norm(key))

    def _handle_for(self, entity_id: uuid.UUID) -> str | None:
        for handle in self._by_handle.values():
            if handle.entity.id == entity_id:
                return handle.handle
        return None

    # --- the note --------------------------------------------------------------

    async def _load_note(self, session: AsyncSession) -> list[_ChunkRef]:
        """The note's paragraph chunks — the citation anchors and the haystack a quote
        is checked against. Loaded once per conversation: a re-ingest replaces them, and
        a re-ingest opens a NEW conversation."""
        if self._chunks is None:
            rows = (
                await session.execute(
                    select(Chunk.id, Chunk.text)
                    .where(Chunk.note_id == self._target.note_id, Chunk.granularity == PARAGRAPH)
                    .order_by(Chunk.seq)
                )
            ).all()
            self._chunks = [_ChunkRef(id=r.id, text=r.text) for r in rows]
            self._note_text = _norm("\n".join(c.text for c in self._chunks))
        return self._chunks

    def _attests(self, quote: str) -> bool:
        """Whether the quote is really in the note. The model cannot claim attestation;
        it can only offer text, and this is the deterministic check on the text."""
        body = _norm(quote)
        return bool(body) and body in self._note_text

    # --- resolve_entity --------------------------------------------------------

    async def resolve_entity(self, arguments: dict, ctx: ToolContext) -> ToolOutput:
        del ctx  # the write session is the note's, never the turn's read scope
        items, clamped = _batch(arguments, ("entities", "surfaces", "items"), MAX_ENTITIES)
        if not items:
            return ToolOutput(
                "resolve_entity takes `entities`: a list of {surface, kind} objects, one per"
                " name the note uses. Nothing was resolved."
            )
        if self.resolve_budget.exhausted:
            return ToolOutput(
                "resolve_entity is out of budget for this note. Work with the handles you"
                " already have, or tell the owner what is left unresolved."
            )
        self.resolve_budget.used += 1

        lines: list[str] = []
        refs: list[EntityRef] = []
        async with scoped_session(self._maker, self._write_ctx) as session:
            chunks = await self._load_note(session)
            for idx, item in enumerate(items):
                surface = _text(item, "surface", "name", "entity")
                if not surface:
                    lines.append(f"err  entities[{idx}]: no surface. Give the name as written.")
                    continue
                known = self.lookup(surface)
                if known is not None:
                    lines.append(f"{known.handle}  {known.label} — already resolved this note")
                    refs.append(_entity_ref(known))
                    continue
                kind = _KIND_HINTS.get(_text(item, "kind", "type").casefold(), _DEFAULT_KIND)
                try:
                    async with session.begin_nested():
                        handle = await self._resolve_one(session, surface, kind, chunks)
                except Exception as exc:  # noqa: BLE001 — one element, not the batch
                    log.warning("graphwrite.resolve_failed", surface=surface, error=repr(exc))
                    lines.append(f"err  entities[{idx}] '{surface}': not resolved (internal).")
                    continue
                if handle is None:
                    lines.append(
                        f"err  entities[{idx}] '{surface}': several of the owner's entities"
                        " share that name, so this is ambiguous. Say which one from the note,"
                        " or leave it out."
                    )
                    continue
                self._remember(handle)
                lines.append(
                    f"{handle.handle}  {handle.label} [{handle.kind}] ({handle.domain}) —"
                    f" {'new entity' if handle.entity.created else 'already known'}"
                )
                refs.append(_entity_ref(handle))
        if clamped:
            lines.append(
                f"note  only the first {MAX_ENTITIES} surfaces were taken; send the rest in a"
                " second call."
            )
        lines.append(f"resolve_entity: {self.resolve_budget.remaining} calls left this note")
        return ToolOutput("\n".join(lines), entities=tuple(refs))

    async def _resolve_one(
        self, session: AsyncSession, surface: str, kind: str, chunks: list[_ChunkRef]
    ) -> Handle | None:
        """Resolve one surface through W1's `commit_facts` — the shipped layered
        resolver, the provisional mint, and the mention spine, in one call with no facts.
        Writing the mention here is what makes `resolve_entity` "writes the mention
        spine": the co-mention graph `repo.neighborhood()` traverses is built from these
        rows, and it is deliberately un-gated by confidence.

        The mention's NAME is the surface, never the handle: `_resolve_entities` resolves
        (and, failing that, MINTS) on `mention.name`, so naming it `e1` would create an
        entity literally called `e1`. The handle is this module's own addressing and must
        never reach the resolver."""
        ref = f"e{len(self._by_handle) + 1}"
        extraction = Extraction(
            title="",
            tags=[],
            mentions=[ExtractedMention(name=surface, kind=kind, surface_text=surface)],
            facts=[],
            tokens=[],
        )
        outcome = await self._pipeline.commit_facts(
            session,
            note_id=self._target.note_id,
            note_domain=self._target.domain,
            captured_at=self._target.captured_at,
            chunks=chunks,
            extraction=extraction,
            extractor=self._extractor,
        )
        entity = outcome.resolved.get(surface)
        if entity is None:
            # The resolver found several live entities on that name (or nothing it could
            # decide). It files its own ambiguity card; the model gets no handle, which
            # is the honest answer — a guess here is a mislinked fact forever.
            return None
        row = (
            await session.execute(
                select(Entity.canonical_name, Entity.kind, Entity.domain_code).where(
                    Entity.id == entity.id
                )
            )
        ).one()
        return Handle(
            handle=ref,
            entity=entity,
            surface=surface,
            kind=row.kind or kind,
            name=row.canonical_name,
            domain=row.domain_code,
            visible=row.domain_code in self._read_scopes,
        )

    # --- assert_fact -----------------------------------------------------------

    async def assert_fact(self, arguments: dict, ctx: ToolContext) -> ToolOutput:
        del ctx
        items, clamped = _batch(arguments, ("facts", "items"), MAX_FACTS)
        if not items:
            return ToolOutput(
                "assert_fact takes `facts`: a list of {subject, predicate, object, statement,"
                " when, quote} objects. Nothing was recorded."
            )
        if self.assert_budget.exhausted:
            return ToolOutput(
                "assert_fact is out of budget for this note. Say what is left unrecorded"
                " rather than trying again."
            )
        self.assert_budget.used += 1

        lines: list[str] = []
        refs: list[EntityRef] = []
        writes: list[FactWriteRef] = []
        async with scoped_session(self._maker, self._write_ctx) as session:
            chunks = await self._load_note(session)
            for idx, item in enumerate(items):
                try:
                    async with session.begin_nested():
                        line, write, touched = await self._assert_one(session, idx, item, chunks)
                except Exception as exc:  # noqa: BLE001 — one element, not the batch
                    log.warning("graphwrite.assert_failed", index=idx, error=repr(exc))
                    lines.append(f"err  facts[{idx}]: not recorded (internal).")
                    continue
                lines.append(line)
                if write is not None:
                    writes.append(write)
                refs.extend(touched)
        if clamped:
            lines.append(
                f"note  only the first {MAX_FACTS} facts were taken; send the rest in a"
                " second call."
            )
        lines.append(f"assert_fact: {self.assert_budget.remaining} calls left this note")
        return ToolOutput("\n".join(lines), entities=tuple(refs), facts=tuple(writes))

    # --- correct_fact ----------------------------------------------------------

    def adopt(
        self,
        *,
        entity_id: uuid.UUID,
        subject_id: uuid.UUID | None,
        surface: str,
        name: str,
        kind: str,
        domain: str,
    ) -> Handle:
        """Register an entity the caller resolved ELSEWHERE as a handle of this writer.

        `resolve_entity` stays the only MINTING path — this adopts a row that already
        exists and that the caller located under the turn's own read scopes, so nothing
        here can create an entity. It is what lets `correct_fact` reuse `_assert_one`
        whole: the write path addresses subjects by handle, and the on-reply turn earns
        its entity from `find_entity`/`read_entity` rather than from a handle table that
        lives in another process."""
        handle = Handle(
            handle=f"e{len(self._by_handle) + 1}",
            entity=ResolvedEntity(id=entity_id, subject_id=subject_id),
            surface=surface,
            kind=kind,
            name=name,
            domain=domain,
            visible=domain in self._read_scopes,
        )
        self._remember(handle)
        return handle

    async def correct_fact(self, item: Mapping[str, Any]) -> ToolOutput:
        """Write ONE owner correction: force-supersede the address's current head(s) and
        pin the new value (D11).

        Not batched, unlike its two siblings, and that is the design rather than an
        omission: a correction is one thing the owner disputed, addressed by one identity
        key the handler had to disambiguate against the graph first. Batching it would
        make the multi-row retry — the whole of the addressing design — a per-element
        conversation inside one result.

        The write is `_assert_one` with `correction=True`, so every mechanic is the
        shipped one: the same resolver override, the same `commit_facts`, the same
        `decide()`, the same domain floor and ratchet, the same result vocabulary."""
        if self.correct_budget.exhausted:
            return ToolOutput(
                "correct_fact is out of budget for this note. Tell Jeff what is still"
                " wrong rather than trying again."
            )
        self.correct_budget.used += 1
        async with scoped_session(self._maker, self._write_ctx) as session:
            chunks = await self._load_note(session)
            try:
                async with session.begin_nested():
                    line, write, touched = await self._assert_one(
                        session, 0, item, chunks, correction=True
                    )
            except Exception as exc:  # noqa: BLE001 — a failed correction is text, not a crash
                log.warning("graphwrite.correct_failed", error=repr(exc))
                return ToolOutput(
                    "correct_fact could not record that (internal). Nothing changed —"
                    " say so rather than telling Jeff it is fixed."
                )
        lines = [line.replace("facts[0]", "correct_fact", 1)]
        lines.append(f"correct_fact: {self.correct_budget.remaining} calls left this note")
        return ToolOutput(
            "\n".join(lines),
            entities=tuple(touched),
            facts=(write,) if write is not None else (),
        )

    async def _assert_one(
        self,
        session: AsyncSession,
        idx: int,
        item: Mapping[str, Any],
        chunks: list[_ChunkRef],
        *,
        correction: bool = False,
    ) -> tuple[str, FactWriteRef | None, list[EntityRef]]:
        subject_token = _text(item, "subject", "entity", "about")
        subject = self.lookup(subject_token)
        if subject is None:
            hint = f' "{subject_token}"' if subject_token else ""
            return (
                f"err  facts[{idx}].subject{hint}: no such handle. resolve_entity first.",
                None,
                [],
            )
        predicate = _text(item, "predicate", "relation", "property")
        if not predicate:
            return f"err  facts[{idx}]: no predicate. Say what relation this fact is.", None, []
        statement = _text(item, "statement", "sentence")
        literal = _text(item, "object", "value")
        if not literal:
            return (
                f"err  facts[{idx}]: no object. Give the other side of the relation — a"
                " handle for an entity, or the value itself.",
                None,
                [],
            )
        # A predicate the registry knows is rewritten to its canonical spelling BEFORE
        # anything reads it, and a qualifier the model folded into the dotted path is
        # recovered. This is where D18's bounded model-chosen domain actually lands:
        # `domain_floor` is keyed on the predicate, so normalizing first is what stops a
        # sensitive predicate being written into a general note under a spelling the
        # floor does not recognise.
        registry = get_registry()
        # `qualifier` is only ever supplied by `correct_fact`, whose identity key is
        # (entity, predicate, qualifier) — `assert_fact`'s schema has no such field and
        # reads "", which is `decompose_predicate`'s dotted-path recovery case unchanged.
        predicate, qualifier = registry.decompose_predicate(
            predicate, _text(item, "qualifier", "of", "for")
        )

        obj = self.lookup(literal)
        object_ref = obj.surface if obj is not None else None
        value_json = None if obj is not None else _quantity_value(literal)
        notes: list[str] = []

        temporal: ExtractedTemporal | None = None
        when = _text(item, "when", "date", "time")
        if when:
            try:
                temporal = _temporal(when, self._target.anchor, self._target.tz_offset_minutes)
            except ValueError:
                notes.append(f'when "{when}" is not a date — recorded undated')

        # An owner CORRECTION carries no `quote` and is never weight-capped: the passage
        # it rests on is the owner's own message, which is not in the note's chunks when
        # the tool runs (the clarification block's re-ingest is asynchronous), so a quote
        # check here could only ever fail and would cap every correction at the inferred
        # ceiling — the one weight that cannot overwrite the value being corrected. Its
        # attestation is WHO SPOKE, which is a property of the tool being bound at all.
        if correction:
            attested, signals = True, _ATTESTED
        else:
            quote = _text(item, "quote", "span", "evidence")
            attested = self._attests(quote)
            if not attested:
                notes.append(
                    "quote is not in the note — recorded, but at low weight so it cannot"
                    " overwrite anything"
                )
            signals = _ATTESTED if attested else _UNATTESTED
        confidence = effective_weight(1.0, signals)

        if not statement:
            statement = _statement(subject.label, predicate, obj.label if obj else literal)
        fact = ExtractedFact(
            predicate=predicate,
            qualifier=qualifier,
            kind=_fact_kind(registry, subject.kind, predicate, object_present=obj is not None),
            statement=statement,
            value_json=value_json,
            assertion="asserted",
            # The SURFACE, never the handle: a ref reaches `_resolve_entities`, which mints
            # on a name it cannot resolve — an `e1` ref that missed the override would
            # create an entity called "e1". The override below is keyed the same way, so
            # nothing here is ever re-resolved.
            entity_ref=subject.surface,
            object_entity_ref=object_ref,
            temporal=temporal,
            # NEVER a model-supplied domain (the firewall red-team rule). The note's
            # domain is the floor's input; `_upsert_fact` applies `domain_floor` and then
            # the asymmetric ratchet, so a clinical predicate in a general note lands in
            # `health` and nothing can move a fact DOWN out of its note's domain.
            domain=self._target.domain,
            confidence=confidence,
            self_confidence=1.0,
            # Recomputed, never asserted by the model (TOOL_SURFACE: no `inferred` field).
            inferred=not attested,
            # D11: the ONE field `assert_fact` deliberately withholds and `correct_fact`
            # sets. `supersession.decide()` reads it and, on a single-head address,
            # supersedes every current head and commits active + PINNED regardless of
            # temporal order — the force-supersede + pin the retired correction-note path
            # had. It stays `decide()`'s branch, not a second write path here.
            correction=correction,
        )
        # Re-assert the mentions this fact hangs off, so the fact's citation anchors where
        # its subject is NAMED rather than on the note's first chunk. Idempotent:
        # `_upsert_mentions` matches on (chunk, span, entity) and keeps the existing row's
        # id, which is what lets the settle reconcile spare it.
        mentions = [
            ExtractedMention(name=h.surface, kind=h.kind, surface_text=h.surface)
            for h in (subject, obj)
            if h is not None
        ]
        override: dict[str, ResolvedEntity | None] = {
            h.surface: h.entity for h in (subject, obj) if h is not None
        }
        extraction = Extraction(title="", tags=[], mentions=mentions, facts=[fact], tokens=[])
        outcome = await self._pipeline.commit_facts(
            session,
            note_id=self._target.note_id,
            note_domain=self._target.domain,
            captured_at=self._target.captured_at,
            chunks=chunks,
            extraction=extraction,
            extractor=self._extractor,
            resolution_override=override,
        )
        write = outcome.writes.get(0)
        if write is None:
            return (
                f"err  facts[{idx}]: {subject.label} could not be linked, so nothing was"
                " recorded. Resolve the subject again.",
                None,
                [],
            )
        refs = [_entity_ref(h) for h in (subject, obj) if h is not None]
        return (
            _write_line(idx, subject.label, predicate, obj.label if obj else literal, write, notes),
            FactWriteRef(
                fact_id=str(write.fact_id),
                label=write.statement,
                domain=write.domain,  # type: ignore[arg-type]  # validated by the write path
                outcome=write.outcome,
            ),
            refs,
        )


def _write_line(
    idx: int, subject: str, predicate: str, value: str, write: FactWrite, notes: Sequence[str]
) -> str:
    """One fact's landing, in the result shape TOOL_SURFACE specifies: the identity key,
    then what the SERVER did that the model did not ask for."""
    head = f"{'held' if write.outcome == HELD else 'ok'}  {subject}.{predicate} → {value}"
    tail: list[str] = list(notes)
    if write.outcome == REPLACED and write.replaced:
        tail.insert(0, f"replaced {'; '.join(write.replaced)}, kept as history")
    elif write.outcome == HELD:
        clash = f" with {write.conflicting}" if write.conflicting else ""
        reason = write.hold_reason or "unresolved"
        tail.insert(
            0,
            f"clashes{clash} ({reason}) — recorded but NOT live. Ask the owner which is right",
        )
    elif write.outcome in _OUTCOME_WORDS:
        tail.insert(0, _OUTCOME_WORDS[write.outcome])
    if write.domain != "general":
        tail.append(f"filed under {write.domain}")
    return head + (f" — {'; '.join(tail)}" if tail else "")


def _statement(subject: str, predicate: str, value: str) -> str:
    """The rendered sentence when the model gave none. Deterministic and dull on
    purpose: it is what the wiki and the review cards print, so a missing statement must
    still read as a sentence rather than as a row of raw fields."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", predicate.replace("_", " ").replace(".", " "))
    return f"{subject} {words.lower().strip()}: {value}"


def _fact_kind(registry: Any, entity_kind: str, predicate: str, *, object_present: bool) -> str:
    """The fact kind, from the registry where it declares one. An edge to another entity
    is a relationship whatever else it looks like; an undeclared (tier-2) predicate falls
    back to the entity type's own default, then to `attribute` — the kind whose
    supersession floor is the most cautious (a collision goes to review, never an
    auto-overwrite)."""
    if object_present:
        return "relationship"
    declared = registry.predicate_for_kind(entity_kind, predicate)
    if declared is not None and declared.kind:
        return str(declared.kind)
    entity_type = registry.by_kind.get(entity_kind)
    if entity_type is not None and entity_type.default_fact_kind:
        return str(entity_type.default_fact_kind)
    return "attribute"


def _entity_ref(handle: Handle) -> EntityRef:
    return EntityRef(
        entity_id=str(handle.entity.id),
        label=handle.label,
        domain=handle.domain,  # type: ignore[arg-type]  # a domain code from the row
    )


def _text(item: Mapping[str, Any], *keys: str) -> str:
    """One string field, tolerant of the near-miss key names a model reaches for. Not
    laxity: the schema names one key and the grammar fills it, but a synonym arriving as
    a dropped fact is a silent loss, and there is no cost to accepting it."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _batch(
    arguments: Mapping[str, Any], keys: Sequence[str], cap: int
) -> tuple[list[Mapping[str, Any]], bool]:
    """The batch, clamped. A bare string element (the model sending `["Dana"]` for a
    two-field shape) is lifted into a one-key object rather than dropped, and the clamp
    is REPORTED because `maxItems` is not reliably compiled into llama.cpp's tool
    grammar — the handler is the only real ceiling."""
    raw: Any = None
    for key in keys:
        if isinstance(arguments.get(key), list):
            raw = arguments[key]
            break
    if raw is None:
        return [], False
    items: list[Mapping[str, Any]] = []
    for element in raw:
        if isinstance(element, Mapping):
            items.append(element)
        elif isinstance(element, str) and element.strip():
            items.append({"surface": element.strip(), "subject": element.strip()})
    return items[:cap], len(items) > cap


@dataclass
class NoteToolset:
    """The tools one note conversation runs with: the two graph writes bound to its
    note, plus whatever handlers the caller passes alongside them. Today that is
    find_entity / read_entity / current_time — inherited unchanged, and reached only
    because the persona now reads the knowledge base — and `ask_owner`, which is a write
    but not a note-BOUND one: it finds its conversation through the turn's session id,
    so one handler serves every note and the chat registry too."""

    writer: NoteGraphWriter
    inherited: Mapping[str, ToolHandler] = field(default_factory=dict)

    def handlers(self) -> dict[str, ToolHandler]:
        return {
            RESOLVE_ENTITY: self.writer.resolve_entity,
            ASSERT_FACT: self.writer.assert_fact,
            **dict(self.inherited),
        }


def note_registry(tools_dir: Any, handlers: Mapping[str, ToolHandler]) -> ToolRegistry:
    """A registry holding EXACTLY the named tools' sidecars.

    Not `load_registry`, which globs the whole directory and demands a handler for every
    sidecar in it: a note conversation binds six tools, and building it from the full
    116-sidecar set would either fail startup or drag the entire chat tool surface into
    the worker to serve a persona that may call none of it. Building from the names is
    what makes "this persona reaches no other tool" structural rather than a property of
    one profile field."""
    tools = []
    for name in sorted(handlers):
        path = tools_dir / f"{name}.tool"
        toolfile = load_tool(path)
        if toolfile.spec.name != name:
            raise ValueError(f"{path}: sidecar declares {toolfile.spec.name!r}, expected {name!r}")
        tools.append(RegisteredTool(toolfile=toolfile, handler=handlers[name]))
    return ToolRegistry(tools)
