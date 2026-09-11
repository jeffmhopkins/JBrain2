"""Run a Scenario against a real Postgres through the genuine note-conversation
write path, then snapshot the graph for the checker.

We are the model: each step scripts the `note.extract` response, and the runner
turns it into the TOOL CALLS a faithful agent would make — one batched
`resolve_entity` per twelve surfaces, then the note's whole READING: `close_reading`
carrying the title, the tags and up to eight facts a call
(`jbrain.agent.graphwritetools`). Everything downstream is the real engine:
`commit_facts` resolves the surfaces, anchors the mention spine, and runs every
fact through `supersession.decide()`, the domain floor and the ratchet;
`sweep_note` + `settle_tail` then close the note out ONCE, over the whole reading —
plan constraint 6, the sweep is whole-conversation, never per call. Not
`settle_note`: it also runs the two review-card halves R3 retires, and its
`note_analysis` stamp is R3's to hand the reading. The SWEEP half is no longer a
divergence from production but the shape R3 specifies, and what licenses it is the
reading; `_run_step` says what that rests on and what stays the harness's alone.

**What the harness tests is unchanged: the deterministic engine given good model
output.** What changed is the SHAPE of that output. The old runner compiled an
`IntegrationIntent` and drove `integrate_note`, so it also pinned the arbiter and
the review cards W5 deletes. A scenario failing here still means the ENGINE
changed, not the model — the faithful agent lives in `_tool_calls` and nowhere
else, one function rather than seventy-five files.

**What the tool surface cannot say**, and so what a scenario can no longer
script. Each was a real gap in `assert_fact`, and `close_reading` inherits every
one of them: the reading adds `title` and `tags` and takes the same seven flat
scalars per fact, so the re-cut loosens nothing below. Three are closed and three
are accepted, and every one of the six was decided by putting the candidate schema
in front of the live model (`backend/evals/shape_probe.py`, the `fields` suite)
rather than by argument. The finding that decided them, and the one worth
carrying: **`required` buys presence, not membership.** gpt-oss fills every
required string field every time — with a value it invented. Asked for a fact
`kind` from a six-word list, in an imperative "copy exactly one of these words
and never any other", it wrote `residence`, `employment`, `medical`: 7 of 80.
Asked for an `assertion` from a five-word list: 0 of 72. The only closed
vocabularies a tool grammar can enforce without a JSON-Schema `enum` (plan
constraint 8) are the JSON types themselves — `number` and `boolean` — and the
two fields that ship are one of each shape or a plain ISO date.

  - **No `qualifier`, and none is coming (accepted).** Probed as a required
    field, the model filled it with prose on 61 of 86 facts ("previous weight
    182 lb in March", "vehicle no longer owned"), and an over-applied qualifier
    splits an identity key so nothing supersedes again — strictly worse than the
    collision it was meant to fix. What EXISTS is the dotted path
    `registry.decompose_predicate` already reads and the reading's own
    `predicate` description teaches: `name.nickname.friends` stores as
    name.nickname + friends, bounded to the
    five registry predicates declaring a `qualifier_vocab`. `_predicate` below
    folds there and nowhere else. A long-tail qualifier is still dropped, so two
    scalar facts under one undeclared predicate still collide. And the channel is
    OPEN but unreached: this synthesiser uses it because it is a perfect model,
    while the live one wrote `has nickname` where the registry declares
    `name.nickname` and carried a third segment 0 times in 39.
  - **No `assertion` but `asserted` (accepted).** A future date still normalizes
    to `expected` and a past marker in the statement still closes the interval —
    both are `_upsert_fact`'s own normalizers, and they still fire. A NEGATED
    fact ("I sold the Civic") has no expression at all, so a disposal stated in
    a LATER note cannot reach the earlier note's fact; the settle sweep only
    retracts facts of the note it is settling. The measurement is above; the
    owner's `correct_fact` on the reply turn is the channel that survives.
  - **No structured `value_json` (accepted).** `object` is a string, so a
    literal value is stored as `{value}` or `{value, unit}`. Anything richer — a
    nested payload, a 13-key flat object — is flattened by `_object_literal`
    before it ever reaches the tool, and an edge with an object entity stores no
    `value_json` at all. The deliberate narrowing TOOL_SURFACE gap 5 states: the
    model is never asked to nest.
  - **An interval END is sayable (closed).** `when_end` is the reading's seventh
    flat scalar, and `_when_end` below scripts it. The model fills it and closes the
    one genuinely-closed interval in a note — and stamps an end on nearly every
    other fact too, so `graphwritetools._close_interval` refuses an end that is
    not a date, has no start, or does not pass the start's own period.
  - **No `kind` (accepted).** `_fact_kind` derives it: an object edge is always
    `relationship`, and everything else falls to the registry's declaration for
    the predicate, then the subject type's default, then `attribute`. A
    `measurement` time-series and a `preference` are not sayable on an
    undeclared predicate, and asserting `kind: relationship` on an object edge
    is now a tautology. Closing it means DECLARING the predicate, not asking the
    model.
  - **No `confidence` self-report — closed, then DELETED (accepted).** It shipped
    as a JSON `number` in v3 and R1b took it out again on R0's measurement: 1
    silent guess in 106 runs, and the guess came from the arm that HAS the field.
    What tipped it was the cost side rising under one channel — a spurious low
    number parks a TRUE fact behind a hold that no card will ever raise. So the
    engine's span check is the whole weight, and the faithful agent below sends
    no such field, because the live model has none to send. A scenario cannot
    script a self-report at all; `ExtractedFact.confidence` still exists and is
    still what the ANALYZER's path reads, which is why the goldens keep it.
  - **No arbiter (accepted).** `derive_kinship_gender` and the rest of the
    arbiter's derivations do not run on this path, so facts main inferred are
    simply absent (`rel_enumerated_children_fan_out`: 8 facts where main wrote
    12).

**What the READING adds.** Two things `assert_fact` has no verb for, and the harness
sends both. The note's `title` and `tags` ride the first call, which is what lets a
settle stamp `note_analysis` from a conversation at all (R3's third step). And a
repeating schedule is read out of each fact's ATTESTED QUOTE rather than off a field
(`_assert_one(read_recurrence=True)`, on R0's 0-in-228 measurement that no `repeats`
field is fillable). The harness quotes its subject's own `surface_text`, which is a
name and not a schedule, so the faithful default reaches `parse_recurrence` on no
scenario in the suite — a scenario wanting an RRULE off the quote has to author one.

    That gap is DELIBERATE here and covered elsewhere, which review confirmed:
    `test_note_graph_write_pg.py` drives the recurrence read directly. Closing it in
    this suite means aiming a quote at the schedule — `plan_recurring_gym` is one
    string away, its body carrying "gym every Monday at 6am" verbatim — and that is
    NEW COVERAGE, which this wave's acceptance ("every green scenario stays green,
    nothing changes state") exists to keep out. It is the synthesiser being unfaithful
    on exactly one behaviour, though, and `close_reading.tool` tells the model the
    opposite ("include the words that say WHEN or HOW OFTEN"), so it is worth a wave
    of its own rather than a footnote forever.

Usable two ways:
  - pytest (tests/integration/test_harness_scenarios.py) drives run_scenario
    against the shared testcontainers database fixture.
  - CLI for interactive "be the model" work against a standing DB (see
    scripts/llm-harness.sh):
      python -m tests.harness.runner prompt    # print the real assembled prompt
      python -m tests.harness.runner run FILE   # run one scenario, print result
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.graphwritetools import (
    MAX_ENTITIES,
    MAX_FACTS,
    NoteGraphWriter,
    NoteTarget,
)
from jbrain.agent.loop import ToolContext
from jbrain.analysis.entities import ResolvedEntity, get_or_create_me
from jbrain.analysis.extraction import ExtractedFact, Extraction, parse_extraction
from jbrain.analysis.pipeline import AnalysisPipeline, CommitOutcome, local_anchor
from jbrain.analysis.prompt import fact_cap
from jbrain.analysis.settle_owner import CONVERSATION
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.queue import SYSTEM_CTX
from jbrain.schema import get_registry
from tests.harness.scenario import (
    EntityRow,
    FactRow,
    ReviewRow,
    Scenario,
    Snapshot,
    Step,
    check,
    load_scenario,
)

# What the note conversation stamps on the facts it writes — `converse.py`'s
# default, so a harness row is indistinguishable from a live one.
EXTRACTOR = "note_ingest"


# --- the conversation ledger -----------------------------------------------


@dataclass
class _Ledger:
    """The union of every `commit_facts` pass one note's conversation made — the parts
    of it the READING does not carry.

    The accumulation seam plan constraint 6 names: `settle_note` is whole-note, so
    settling on a single tool call's share would retract what the earlier calls
    committed. `touched` is no longer among these fields: the facts a sweep must spare
    are `Reading.fact_ids`, which is the reading's own claim rather than an accumulator
    only this process has, and `_run_step` sweeps from there. What is left is what
    production reads back from the 0191 ledger (`NoteConversationRepo.writes()` —
    entities) plus the mention ids nobody durably records."""

    resolved: dict[str, ResolvedEntity | None] = field(default_factory=dict)
    projected: set[uuid.UUID] = field(default_factory=set)
    mention_ids: set[uuid.UUID] = field(default_factory=set)


class _LedgerPipeline(AnalysisPipeline):
    """The real pipeline, with every `commit_facts` outcome recorded.

    A subclass rather than an accumulator inside `NoteGraphWriter`: the writer
    accumulates nothing today, and the durable ledger that will feed production's
    settle is a later task's. Overriding here keeps the harness honest about
    which half is shipped — every line of write behaviour below this override is
    the shipped one."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], router: LlmRouter) -> None:
        super().__init__(maker, router)
        self.ledger = _Ledger()

    async def commit_facts(self, session: AsyncSession, **kwargs: Any) -> CommitOutcome:
        outcome = await super().commit_facts(session, **kwargs)
        led = self.ledger
        led.resolved.update(outcome.resolved)
        led.projected |= outcome.projected
        led.mention_ids |= outcome.mention_ids
        return outcome


def _pipeline(maker: async_sessionmaker[AsyncSession]) -> _LedgerPipeline:
    """A pipeline with no live model behind it. The write path makes no LLM call
    at all — resolution layer 2 is skipped when no embedder is wired — so the
    router exists only to satisfy the constructor."""
    return _LedgerPipeline(maker, LlmRouter({"xai": FakeLlmClient([])}, {}))


# --- the faithful agent: one extraction becomes one turn of tool calls -------
#
# The turn is `resolve_entity` then `close_reading`, which is how a pass RECORDS a note
# now (R1): the reading is the model restating the whole note — title, tags and every
# fact — rather than a run of incremental writes that says nothing about what was read.


def _object_literal(fact: ExtractedFact) -> str:
    """The `object` string for a fact with no object entity.

    `object` is a plain string on both write tools and the handler rebuilds
    `{value}` / `{value, unit}` from it, so a structured `value_json` has to be
    rendered down before it can be read back. A `value`/
    `unit` pair round-trips through the tool's quantity parser; a single-key dict
    gives its value; anything else is its non-boolean values in order, which is
    roughly what a model reading the same sentence would have written.

    **Nothing here is shaped by what the engine needs to see.** This function is
    the model stand-in, and the harness's whole contract is that it scripts a
    PERFECT model so a failure means the engine changed. A branch tuned so the
    engine's dedup would compare equal — there was one, re-spelling `{"kg": 80.0}`
    as "80.0 kg" because dropping the unit made two spellings of one weight
    incomparable — breaks that contract: it hides an engine gap behind a
    sympathetic stand-in. The unit is dropped, and the scenarios that then fail
    say so in their `xfail` (hist_backdated_measurement_insert)."""
    value = fact.value_json
    if isinstance(value, dict) and value:
        if "value" in value:
            unit = value.get("unit")
            return f"{value['value']} {unit}" if unit else str(value["value"])
        if len(value) == 1:
            return str(next(iter(value.values())))
        parts = [
            str(v) for v in value.values() if v is not None and not isinstance(v, bool) and v != []
        ]
        if parts:
            return " ".join(parts)
    if value is not None:
        return json.dumps(value, ensure_ascii=False)
    # No value and no object entity: the sentence is all the note gives.
    return fact.statement


def _when(fact: ExtractedFact) -> str:
    """The `when` string — the ISO START the note gave, or empty."""
    temporal = fact.temporal
    if temporal is None or temporal.resolved_start is None:
        return ""
    return temporal.resolved_start.isoformat()


def _when_end(fact: ExtractedFact) -> str:
    """The `when_end` string — the ISO END the note gave, or empty. The reading
    carries an interval end as a seventh flat scalar (v3's field, TOOL_SURFACE gap 4), so
    a note that states a CLOSED interval in one sentence no longer needs a later
    note to close it. Empty is the overwhelmingly common answer, and the tool's
    handler refuses an end that has no start, does not parse, or does not follow
    its start — the three shapes `evals/shape_probe.py` measured the live model
    producing."""
    temporal = fact.temporal
    if temporal is None or temporal.resolved_end is None:
        return ""
    return temporal.resolved_end.isoformat()


def _predicate(fact: ExtractedFact) -> str:
    """The `predicate` string, with a qualifier folded into the dotted path where
    the registry says the predicate takes one.

    No write tool has a `qualifier` field and none is getting one (TOOL_SURFACE
    gap 3): the live model fills a qualifier field with a date, a phrase or the
    object's own name on most of the facts in a note, and an over-applied
    qualifier splits an identity key so nothing ever supersedes again. What it
    HAS is the channel `registry.decompose_predicate` already reads and the
    reading's own `predicate` description teaches — `name.nickname.friends` is stored as
    name.nickname + friends. That channel is only open for the five registry
    predicates declaring a `qualifier_vocab`, so this folds there and nowhere
    else: a long-tail qualifier is still dropped, and the scenarios that then
    collide still say so in their `xfail`.

    The round trip is the test, not a spelling rule — if the registry does not
    recover the segment, the dotted form would land as a NOVEL predicate, which
    would separate the two facts by corrupting the key rather than qualifying
    it."""
    if not fact.qualifier:
        return fact.predicate
    dotted = f"{fact.predicate}.{fact.qualifier}"
    _, recovered = get_registry().decompose_predicate(dotted, "")
    return dotted if recovered == fact.qualifier else fact.predicate


def _tool_calls(extraction: Extraction, step: Step) -> tuple[list[dict], list[dict]]:
    """The `resolve_entity` batches and the `close_reading` calls a faithful agent would
    send for this step — the ONE place the harness plays the model.

    Surfaces are the extraction's mention NAMES, in first-reference order, plus
    any name a fact refers to without a mention of its own. The name and not the
    `surface_text`, because the name is the addressing the model itself chose:
    the surface is often a verb or a bare pronoun ("Bought", "tonight", "my"),
    and resolving on it would mint an entity called "Bought". The kind is passed
    lowercased and straight through — a word `resolve_entity`'s hint table does
    not know degrades to `Thing` inside the tool, and the harness must show that
    rather than translate around it.

    Facts follow in extraction order, each quoting its subject's own
    `surface_text` — the span the old intent attested with, so "attested" means
    here exactly what it meant before.

    The reading's `title` and `tags` are the extraction's own, unaltered: the scripted
    `note.extract` response is what a perfect model read out of this note, and the
    reading is that same model saying it back. Nothing in the harness reads them (no
    `stamp_analysis` on this path), and they are sent anyway because a reading without
    them is not a call the live agent makes."""
    surface_by_name = {m.name: m.surface_text for m in extraction.mentions}
    kind_by_name = {m.name: m.kind for m in extraction.mentions}
    body_quote = next(iter(surface_by_name.values()), step.body[:24])

    refs: list[str] = []
    for mention in extraction.mentions:
        if mention.name not in refs:
            refs.append(mention.name)
    for fact in extraction.facts:
        for ref in (fact.entity_ref, fact.object_entity_ref):
            if ref and ref not in refs:
                refs.append(ref)

    entities = [
        {"surface": name, "kind": kind_by_name.get(name, "thing").strip().casefold()}
        for name in refs
    ]
    facts = [
        {
            "subject": fact.entity_ref,
            "predicate": _predicate(fact),
            "object": fact.object_entity_ref or _object_literal(fact),
            "statement": fact.statement,
            "when": _when(fact),
            "when_end": _when_end(fact),
            "quote": surface_by_name.get(fact.entity_ref) or body_quote,
        }
        for fact in extraction.facts
    ]
    return _calls(entities, facts, title=extraction.title, tags=extraction.tags)


def _calls(
    entities: list[dict], facts: list[dict], *, title: str, tags: Sequence[str]
) -> tuple[list[dict], list[dict]]:
    """Split the surfaces and the reading at the two tools' own ceilings. The batch is
    the measured shape (20/20 well-formed at ~9 entities and ~8 facts a turn), so a note
    that needs more sends a second call rather than a flat one-per-call turn.

    Title and tags ride the FIRST reading call and no other. That is the call that read
    the note from the top — `Reading.union` keeps the first non-empty title for exactly
    that reason — and the tool's own text tells a continuation call to "send the next 8"
    rather than restate the heading.

    A note that asserts NOTHING still closes its reading. A step with no facts is not a
    step that skipped the tool: it is the model saying the note says nothing, which is
    the one claim a sweep acts on destructively (`rerun_retracts_removed_fact`), and
    emitting no call at all would leave the harness scripting a pass that never read."""
    readings: list[dict[str, Any]] = [
        {"facts": facts[i : i + MAX_FACTS]} for i in range(0, len(facts), MAX_FACTS)
    ] or [{"facts": []}]
    readings[0] = {"title": title, "tags": list(tags), **readings[0]}
    return (
        [
            {"entities": entities[i : i + MAX_ENTITIES]}
            for i in range(0, len(entities), MAX_ENTITIES)
        ],
        readings,
    )


def _authored_calls(step: Step) -> tuple[list[dict], list[dict]]:
    """A scenario that scripts its own tool arguments — `{"entities": [...], "reading":
    {...}}`. Only needed when the faithful default cannot express the case under test: a
    deliberately fumbled `quote`, an object the model chose to leave as a literal.

    Authored against the READING like every other step, rather than kept on the old
    two-call shape. An authored block exists to fumble ONE argument the default gets
    right, and leaving it addressed to a tool the agent no longer calls would have meant
    the fumble was no longer tested on any path the agent takes.

    `title`/`tags` fall back to the step's scripted extraction: the block scripts the
    CALL, not what the note is about, and an untitled reading is not a shape the live
    model produces."""
    calls = step.tool_calls or {}
    reading: Mapping[str, Any] = calls.get("reading", {})
    extraction = step.extraction
    return _calls(
        list(calls.get("entities", [])),
        list(reading.get("facts", [])),
        title=str(reading.get("title", extraction.get("title", ""))),
        tags=[str(t) for t in reading.get("tags", extraction.get("tags", []))],
    )


def _parse_extraction(step: Step) -> Extraction:
    """Lower the note's scripted extraction into an `Extraction` through the genuine
    parse (dedup, fact-cap, drop-invalid), so the tool calls reflect extraction-layer
    behaviour rather than the raw scripted JSON.

    R4 deleted `note.extract` — the prompt, its schema and the `_extract_note` call that
    wrapped this parse — so the scripted JSON is parsed DIRECTLY instead of round-tripping
    through a faked model call. Byte-identical for a harness step: one body block is one
    group, `merge_extractions` passes a single part through untouched, and the group's cap
    is `fact_cap(step.body)`. The scenario format still authors a `note.extract` payload;
    re-cutting it onto the reading's own shape is §5's outstanding item, and until then
    this parse is the last live reader of it."""
    created = datetime.fromisoformat(step.created_at)
    offset = created.utcoffset()
    tz = int(offset.total_seconds() // 60) if offset is not None else None
    parse_anchor = local_anchor(created, tz) if tz is not None else None
    return parse_extraction(step.extraction, anchor=parse_anchor, max_facts=fact_cap(step.body))


# --- the note ---------------------------------------------------------------


@dataclass(frozen=True)
class _Note:
    """A seeded note, carried across steps so a `reanalyze_step` re-runs against
    the same row, the same chunk and the same capture instant."""

    note_id: uuid.UUID
    domain: str
    created_at: datetime
    tz_offset_minutes: int | None


async def _seed_note(maker: async_sessionmaker[AsyncSession], step: Step) -> _Note:
    """Insert one note + a single chunk (body verbatim) with the step's exact
    created_at — reported_at and the temporal anchor every assertion turns on.
    One chunk keeps span-anchoring deterministic; chunk splitting is covered by
    the ingest tests, not here."""
    note_id = uuid.uuid4()
    created = datetime.fromisoformat(step.created_at)
    # Carry the step's local offset like a real capture would: the pipeline's
    # local_anchor (and the backward-phrase repair that rides it) needs it, and
    # without it an evening-capture scenario would resolve against the UTC day.
    offset = created.utcoffset()
    tz_offset = int(offset.total_seconds() // 60) if offset is not None else None
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body, created_at,"
                " tz_offset_minutes) VALUES (:i, :c, :d, :b, :t, :tz)"
            ),
            {
                "i": str(note_id),
                "c": str(note_id)[:12],
                "d": step.domain,
                "b": step.body,
                "t": created,
                "tz": tz_offset,
            },
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:i, :n, :d, 'paragraph', 1, :b)"
            ),
            {"i": str(uuid.uuid4()), "n": str(note_id), "d": step.domain, "b": step.body},
        )
        await s.commit()
    return _Note(note_id, step.domain, created, tz_offset)


async def _run_step(maker: async_sessionmaker[AsyncSession], step: Step, note: _Note) -> None:
    """One note's whole conversation: the surfaces resolved, the reading closed, then
    one settle over that reading.

    The writer is built per note, exactly as `converse.executor_for_note` builds
    it — full-owner write scope (layer 1 of resolution carries no domain
    predicate, and a floored write would be refused outright) with the narrowed
    `(note_domain, 'general')` passed separately, which is what decides whether a
    cross-domain entity's NAME comes back in the result text."""
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await get_or_create_me(session)

    pipeline = _pipeline(maker)
    read_scopes = (note.domain, "general")
    writer = NoteGraphWriter(
        maker,
        pipeline,
        target=NoteTarget(
            note_id=note.note_id,
            domain=note.domain,
            captured_at=note.created_at,
            tz_offset_minutes=note.tz_offset_minutes,
        ),
        write_ctx=SYSTEM_CTX,
        read_scopes=read_scopes,
        extractor=EXTRACTOR,
    )
    ctx = ToolContext(session=SYSTEM_CTX, scopes=read_scopes)

    if step.tool_calls is not None:
        resolves, readings = _authored_calls(step)
    else:
        resolves, readings = _tool_calls(_parse_extraction(step), step)
    for arguments in resolves:
        await writer.resolve_entity(arguments, ctx)
    for arguments in readings:
        await writer.close_reading(arguments, ctx)

    # One settle per note, over the whole reading — never per call (plan constraint 6).
    # An EMPTY reading is passed through deliberately: here it means the conversation
    # read the note and found it says nothing, which is exactly when the note's mentions
    # and facts should be swept. What constraint 7 forbids is a per-CALL settle, and
    # this is not one.
    #
    # NOT `settle_note` whole: that also runs the two review-card halves, which belong to
    # the producers that FILE those cards and which the conversation's settle does not
    # run. Its `note_analysis` stamp is production's now (R3 hands it the reading's title
    # and tags) and is skipped here only because no scenario reads the row.
    #
    # The `sweep_note` below is the SPEC, and it was a divergence until the reading
    # existed. Production's `settle_conversation` runs exactly this since R3 —
    # `sweep_note` off the reading, then the tail — and the reason a conversation sweep
    # was dropped once (SETTLE_OWNERSHIP.md S3) is the reason no LEDGER can license a
    # release: a ledger records what a producer WROTE, so a pass that read the note and
    # wrote nothing is indistinguishable from one that never looked, and the only claims
    # a release could then remove are another session's. That argument is closed and
    # still stands.
    #
    # What it demands is a producer that RE-DERIVED the note and dropped X, and that is
    # what `close_reading` is (plan §1): the model restates the whole note, so
    # `Reading.fact_ids` is the complete current reading a retraction needs. Hence
    # `touched` below comes off the reading rather than off the write ledger this runner
    # used to union — the sweep is licensed by the same claim R3 will gate
    # `settle_conversation` on, running here ahead of it.
    #
    # Note what still does NOT license it: the harness's own in-process accumulator. Read
    # that way it would license a sweep for a single production session too, which is the
    # inference S3 was removed to block — and it is false here anyway, since `run_scenario`
    # reuses a note across steps while `_run_step` builds a fresh writer and pipeline per
    # step, so both the reading and `led` cover THIS step only. That is not a weakness:
    # one step IS one whole-note re-derivation, which is the invariant the sweep wants.
    # It is why `rerun_retracts_removed_fact.json` works — step 2 re-reads the note, its
    # reading names no `homeLocation` fact, and the sweep retracts what step 1 asserted.
    #
    # TWO things here are still not production. R2 named three; R3 closed the middle one
    # in production rather than here. Naming them precisely matters because R4 deletes the
    # old pipeline citing this harness:
    #
    # 1. `mentions`. A reading carries fact ids and no mention ids, so production's settle
    #    passes `mentions=None` and SKIPS the mention reconcile (`sweep_note` says what
    #    that leaks and why it is bounded). The harness HAS those ids in process and
    #    reconciles with them. Its own, and it stays that way.
    #
    # 2. The sweep here is UNGATED. The spec fires it only on a clean, unclamped pass that
    #    produced a reading, and `clarify.settle_conversation` now enforces exactly that;
    #    nothing below consults `writer.reading.clamped`. Inert at the suite's sizes — the
    #    largest step is 6 facts — but the ceiling moved with the verb:
    #    `READING_CALL_BUDGET` 6 x 8 = 48 facts per step, where the retired
    #    `ASSERT_CALL_BUDGET` 10 x 8 gave 80. A 49-fact step would write 48, latch
    #    `clamped`, and be swept anyway, retracting the previous step's tail where
    #    production would refuse.
    #
    # CLOSED, and closed in production: R2's third divergence was that the harness models
    # ONE fact verb where the unattended pass bound two, so a pass could `assert_fact` F
    # and then close a reading that omits F — and a reading-derived sweep would retract a
    # fact that same pass wrote. R3 took `assert_fact` off
    # `agents.NOTE_INGEST_UNATTENDED_TOOLS` (and off the third-party set derived from it)
    # rather than unioning that pass's writes into `touched`, which would have re-admitted
    # the ledger the S3 argument above rejects. One fact verb on the pass is now what
    # production means, so this runner's single verb is faithful rather than a
    # simplification.
    led = pipeline.ledger
    async with scoped_session(maker, SYSTEM_CTX) as session:
        # The harness sweeps as the CONVERSATION — `EXTRACTOR` is `note_ingest` here,
        # and the producer key groups both of that producer's runs.
        retracted = await pipeline.sweep_note(
            session,
            note_id=note.note_id,
            settle_owner=CONVERSATION,
            touched={uuid.UUID(fact_id) for fact_id in writer.reading.fact_ids},
            # In-process, so the harness HAS the mention ids production's ledger does
            # not record — it reconciles where `settle_conversation` must skip.
            mentions=led.mention_ids,
        )
        await pipeline.settle_tail(
            session,
            referenced={e.id for e in led.resolved.values() if e is not None},
            projected=led.projected | retracted,
        )


async def _snapshot(maker: async_sessionmaker[AsyncSession]) -> Snapshot:
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        facts = (
            await s.execute(
                text(
                    "SELECT e.canonical_name AS entity, f.predicate, f.qualifier, f.kind,"
                    " f.assertion, f.status, f.statement, f.value_json,"
                    " f.superseded_by IS NOT NULL AS chained, f.pinned, f.domain_code AS domain,"
                    " f.valid_to IS NOT NULL AS closed"
                    " FROM app.facts f JOIN app.entities e ON e.id = f.entity_id"
                )
            )
        ).all()
        reviews = (
            await s.execute(
                text(
                    "SELECT kind, coalesce(payload->>'summary','') AS summary, status,"
                    " domain_code AS domain FROM app.review_items"
                )
            )
        ).all()
        entities = (
            await s.execute(text("SELECT canonical_name AS name, kind, status FROM app.entities"))
        ).all()
    return Snapshot(
        facts=[
            FactRow(
                entity=r.entity,
                predicate=r.predicate,
                qualifier=r.qualifier,
                kind=r.kind,
                assertion=r.assertion,
                status=r.status,
                statement=r.statement,
                value_json=r.value_json,
                chained=r.chained,
                closed=r.closed,
                pinned=r.pinned,
                domain=r.domain,
            )
            for r in facts
        ],
        reviews=[
            ReviewRow(kind=r.kind, summary=r.summary, status=r.status, domain=r.domain)
            for r in reviews
        ],
        entities=[EntityRow(name=r.name, kind=r.kind, status=r.status) for r in entities],
    )


async def run_scenario(maker: async_sessionmaker[AsyncSession], scenario: Scenario) -> Snapshot:
    """Apply every step in order through the real write path; return the graph."""
    notes: list[_Note] = []
    for step in scenario.steps:
        # Re-analysis of an earlier step's note: same row, same chunk, same
        # reported_at (and same domain) — only the extraction changes, so the
        # conversation is a fresh one over an unchanged note.
        note = notes[step.reanalyze_step] if step.reanalyze_step is not None else None
        if note is None:
            note = await _seed_note(maker, step)
        notes.append(note)
        await _run_step(maker, step, note)
    return await _snapshot(maker)


# --- CLI: interactive "be the model" against a standing DB ------------------


def _print_prompt() -> None:
    from jbrain.analysis.prompt import SYSTEM_PROMPT, build_user_prompt

    body = (
        "Saw Dr. Patel today, BP was 128/82. She wants me back in 3 months. "
        "Bumped into Sarah from accounting — she just moved to Denver."
    )
    anchor = datetime.fromisoformat("2026-06-10T17:11:00-06:00")
    print("================ SYSTEM PROMPT ================")
    print(SYSTEM_PROMPT)
    print("\n================ USER PROMPT (anchor as the model sees it) ====")
    print(build_user_prompt([body], anchor=anchor, domain="general"))


async def _cli_run(url: str, path: str) -> int:
    engine = create_async_engine(url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        scenario = load_scenario(__import__("pathlib").Path(path))
        snap = await run_scenario(maker, scenario)
        for f in snap.facts:
            print(
                f"  {f.entity}.{f.predicate} [{f.kind}/{f.assertion}/{f.status}]"
                f" {f.statement!r} chained={f.chained} closed={f.closed} domain={f.domain}"
            )
        for r in snap.reviews:
            print(f"  REVIEW [{r.kind}/{r.status}] {r.summary} (domain={r.domain})")
        failures = check(snap, scenario.expect)
        if failures:
            print("\nFAIL:")
            for msg in failures:
                print(f"  - {msg}")
            return 1
        print("\nPASS")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    import os

    mode = sys.argv[1] if len(sys.argv) > 1 else "prompt"
    if mode == "prompt":
        _print_prompt()
        return 0
    if mode == "run":
        url = os.environ["JBRAIN_DATABASE_URL"]
        return asyncio.run(_cli_run(url, sys.argv[2]))
    print(f"unknown mode {mode!r}; use 'prompt' or 'run FILE'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
