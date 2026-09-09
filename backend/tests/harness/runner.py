"""Run a Scenario against a real Postgres through the genuine note-conversation
write path, then snapshot the graph for the checker.

We are the model: each step scripts the `note.extract` response, and the runner
turns it into the TOOL CALLS a faithful agent would make — one batched
`resolve_entity` per twelve surfaces, one batched `assert_fact` per eight facts
(`jbrain.agent.graphwritetools`). Everything downstream is the real engine:
`commit_facts` resolves the surfaces, anchors the mention spine, and runs every
fact through `supersession.decide()`, the domain floor and the ratchet;
`settle_note` then closes the note out ONCE, over the union of every call's
writes — plan constraint 6, the sweep is whole-conversation, never per call.

**What the harness tests is unchanged: the deterministic engine given good model
output.** What changed is the SHAPE of that output. The old runner compiled an
`IntegrationIntent` and drove `integrate_note`, so it also pinned the arbiter and
the review cards W5 deletes. A scenario failing here still means the ENGINE
changed, not the model — the faithful agent lives in `_tool_calls` and nowhere
else, one function rather than seventy-five files.

**What the tool surface cannot say**, and so what a scenario can no longer
script. Each is a real gap in `assert_fact`, recorded here because the harness is
where it becomes visible (see the plan's W3 section):

  - **No `qualifier`.** The identity key is `entity.predicate[.qualifier]`
    pointing at a value or another entity, and the tool has no field for the
    middle term. Where the extraction's qualifier named the OBJECT entity
    (`owns.Civic` → Civic) nothing is lost: a non-functional predicate keys on
    its object. Where it discriminated two scalar facts under one predicate (two
    diagnoses in one note, three readings) those facts now collide.
  - **No `assertion` but `asserted`.** A future date still normalizes to
    `expected` and a past marker in the statement still closes the interval —
    both are `_upsert_fact`'s own normalizers, and they still fire. A NEGATED
    fact ("I sold the Civic") has no expression at all, so a disposal stated in a
    LATER note cannot reach the earlier note's fact; the settle sweep only
    retracts facts of the note it is settling.
  - **No structured `value_json`.** `object` is a string, so a literal value is
    stored as `{value}` or `{value, unit}`.

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
from jbrain.analysis.extraction import ExtractedFact, ExtractedMention, Extraction
from jbrain.analysis.pipeline import (
    AnalysisPipeline,
    CommitOutcome,
    _ChunkRef,
    _extract_note,
    local_anchor,
)
from jbrain.db.session import scoped_session
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.queue import SYSTEM_CTX
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
    """The union of every `commit_facts` pass one note's conversation made.

    The accumulation seam plan constraint 6 names: `settle_note` is whole-note,
    so settling on a single tool call's share would retract what the earlier
    calls committed. Production will read this union back from the 0191 ledger
    (`NoteConversationRepo.writes()`); in-process the outcomes are right here, so
    the harness unions them directly rather than pretending to a durable store it
    does not have."""

    resolved: dict[str, ResolvedEntity | None] = field(default_factory=dict)
    touched: set[uuid.UUID] = field(default_factory=set)
    projected: set[uuid.UUID] = field(default_factory=set)
    mention_ids: set[uuid.UUID] = field(default_factory=set)
    mentions: dict[str, ExtractedMention] = field(default_factory=dict)
    facts: list[ExtractedFact] = field(default_factory=list)

    def as_extraction(self) -> Extraction:
        """What the conversation, taken whole, asserted about the note — the
        input `settle_note`'s alias registration and stale-card sweep read. Title
        and tags are empty because the tool surface has no verb for either, and
        `dropped_facts` is 0 because no per-note cap runs on a tool call."""
        return Extraction(
            title="",
            tags=[],
            mentions=list(self.mentions.values()),
            facts=list(self.facts),
            tokens=[],
        )


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
        led.touched |= outcome.touched
        led.projected |= outcome.projected
        led.mention_ids |= outcome.mention_ids
        extraction: Extraction = kwargs["extraction"]
        for mention in extraction.mentions:
            led.mentions.setdefault(mention.name, mention)
        led.facts.extend(extraction.facts)
        return outcome


def _pipeline(maker: async_sessionmaker[AsyncSession]) -> _LedgerPipeline:
    """A pipeline with no live model behind it. The write path makes no LLM call
    at all — resolution layer 2 is skipped when no embedder is wired — so the
    router exists only to satisfy the constructor."""
    return _LedgerPipeline(maker, LlmRouter({"xai": FakeLlmClient([])}, {}))


# --- the faithful agent: one extraction becomes one turn of tool calls -------


def _object_literal(fact: ExtractedFact) -> str:
    """The `object` string for a fact with no object entity.

    `assert_fact` takes a plain string and rebuilds `{value}` / `{value, unit}`
    from it, so a structured `value_json` has to be rendered down. A `value`/
    `unit` pair round-trips through the tool's quantity parser; a single-key dict
    gives its value; anything else is its non-boolean values in order, which is
    roughly what a model reading the same sentence would have written."""
    value = fact.value_json
    if isinstance(value, dict) and value:
        if "value" in value:
            unit = value.get("unit")
            return f"{value['value']} {unit}" if unit else str(value["value"])
        if len(value) == 1:
            key, only = next(iter(value.items()))
            # A one-key dict whose value is a NUMBER is a reading whose key is its
            # unit (`{"kg": 80.0}`, `{"mgdl": 95}`) — a model reading the same
            # sentence writes "80.0 kg", and dropping the unit would make two
            # spellings of one weight incomparable.
            if isinstance(only, (int, float)) and not isinstance(only, bool):
                return f"{only} {key}"
            return str(only)
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
    """The `when` string — the ISO START the note gave, or empty. Only the start:
    the tool has one date field, and `_upsert_fact`'s own normalizers derive the
    future (`expected`) and past-marker (interval close) treatment from it and
    from the statement."""
    temporal = fact.temporal
    if temporal is None or temporal.resolved_start is None:
        return ""
    return temporal.resolved_start.isoformat()


def _tool_calls(extraction: Extraction, step: Step) -> tuple[list[dict], list[dict]]:
    """The `resolve_entity` and `assert_fact` batches a faithful agent would send
    for this step — the ONE place the harness plays the model.

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
    here exactly what it meant before."""
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
            "predicate": fact.predicate,
            "object": fact.object_entity_ref or _object_literal(fact),
            "statement": fact.statement,
            "when": _when(fact),
            "quote": surface_by_name.get(fact.entity_ref) or body_quote,
        }
        for fact in extraction.facts
    ]
    return _batched(entities, facts)


def _batched(entities: list[dict], facts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split both lists at the tools' own ceilings. The batch is the measured
    shape (20/20 well-formed at ~9 entities and ~8 facts a turn), so a note that
    needs more sends a second call rather than a flat one-per-call turn."""
    return (
        [
            {"entities": entities[i : i + MAX_ENTITIES]}
            for i in range(0, len(entities), MAX_ENTITIES)
        ],
        [{"facts": facts[i : i + MAX_FACTS]} for i in range(0, len(facts), MAX_FACTS)],
    )


def _authored_calls(calls: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """A scenario that scripts its own tool arguments. Only needed when the
    faithful default cannot express the case under test — a deliberately fumbled
    `quote`, an object the model chose to leave as a literal."""
    return _batched(list(calls.get("entities", [])), list(calls.get("facts", [])))


async def _parse_extraction(step: Step, domain: str) -> Extraction:
    """Run the note's scripted extraction through the genuine `note.extract`
    parse (dedup, fact-cap, drop-invalid), the same front half the ingest path
    runs, so the tool calls reflect extraction-layer behaviour rather than the raw
    scripted JSON."""
    created = datetime.fromisoformat(step.created_at)
    offset = created.utcoffset()
    tz = int(offset.total_seconds() // 60) if offset is not None else None
    prompt_anchor = local_anchor(created, tz)
    parse_anchor = prompt_anchor if tz is not None else None
    router = LlmRouter(
        {"xai": FakeLlmClient([json.dumps(step.extraction)])},
        {"note.extract": ("xai", "grok-4.3")},
    )
    return await _extract_note(
        router,
        [step.body],
        domain=domain,
        prompt_anchor=prompt_anchor,
        parse_anchor=parse_anchor,
        note_id="harness",
    )


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


async def _chunks(session: AsyncSession, note_id: uuid.UUID) -> list[_ChunkRef]:
    rows = (
        await session.execute(
            text(
                "SELECT id, text FROM app.chunks WHERE note_id = :n"
                " AND granularity = 'paragraph' ORDER BY seq"
            ),
            {"n": str(note_id)},
        )
    ).all()
    return [_ChunkRef(id=r.id, text=r.text) for r in rows]


async def _run_step(maker: async_sessionmaker[AsyncSession], step: Step, note: _Note) -> None:
    """One note's whole conversation: the tool calls, then one settle over their
    union.

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
        resolves, asserts = _authored_calls(step.tool_calls)
    else:
        resolves, asserts = _tool_calls(await _parse_extraction(step, note.domain), step)
    for arguments in resolves:
        await writer.resolve_entity(arguments, ctx)
    for arguments in asserts:
        await writer.assert_fact(arguments, ctx)

    # One settle per note, over the union — never per call (plan constraint 6).
    # An EMPTY union is passed through deliberately: here it means the whole
    # conversation asserted nothing, which is exactly when the note's mentions
    # and facts should be swept. It is a per-CALL settle that constraint 7
    # forbids, and this is not one.
    led = pipeline.ledger
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await pipeline.settle_note(
            session,
            note_id=note.note_id,
            note_domain=note.domain,
            chunks=await _chunks(session, note.note_id),
            extraction=led.as_extraction(),
            extractor=EXTRACTOR,
            resolved=led.resolved,
            touched=led.touched,
            projected=led.projected,
            mention_ids=led.mention_ids,
        )


async def _snapshot(maker: async_sessionmaker[AsyncSession]) -> Snapshot:
    async with maker() as s:
        await s.execute(text("SELECT set_config('app.principal_kind','owner',true)"))
        facts = (
            await s.execute(
                text(
                    "SELECT e.canonical_name AS entity, f.predicate, f.qualifier, f.kind,"
                    " f.assertion, f.status, f.statement, f.value_json,"
                    " f.superseded_by IS NOT NULL AS chained, f.pinned, f.domain_code AS domain"
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
                f" {f.statement!r} chained={f.chained} domain={f.domain}"
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
