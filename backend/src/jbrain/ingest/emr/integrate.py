"""Drive parsed EMR candidates into the graph (docs/plans/EMR_IMPORT_PLAN.md §6.6,
AGENT_INGEST_CONVERSATION_PLAN.md W4/D9).

Lowers each parsed SOURCE into an `IntegrationIntent` and commits it through the
SHIPPED deterministic core — `plan_intent` (validate/weigh/partition) then
`AnalysisPipeline.commit_intent` (resolve entities, per-kind supersession incl. the
§3.5 `_lab_status_transition`, RLS-scoped writes, the kind-guarded projection hook).

**This is the importer/tool boundary, and it is here on purpose (D9, TOOL_SURFACE
gap 3).** The note conversation writes the graph through `assert_fact`, whose schema
has no `fhir_status` field and cannot grow one: it is EMR-only and set by the parser,
`ExtractedFact.fhir_status` is what `supersession._lab_status_transition` reads, and
that transition is how a FHIR *preliminary* reading is kept from becoming a citable
current value (plan constraint 4). So an EMR fact is written by the IMPORTER, straight
into `commit_intent`, never by the model — and the conversation over an EMR note is
narrowed to hold no graph-write verb at all (`emr.ownership`, `agents.narrow_for_emr`),
so there is no second, `fhir_status`-less path onto the same note.

**One note, one settle.** `settle_note` is whole-note: it retracts every non-pinned
fact of the note NOT in `touched` (plan constraint 6). A decrypted EMR archive attaches
MANY PDFs to ONE note and each is parsed separately, so committing them through
`apply_intent` in a loop made each attachment's settle retract the previous
attachment's facts — a two-PDF import kept only the last PDF's readings. `EmrNoteCommit`
is the fix: every source commits through `commit_intent` in its own transaction, the
`touched`/`projected`/`mention_ids` sets, the resolved-entity map and the extractions
accumulate, and ONE `settle_note` runs at the end over their union.

Note the ordering caveat that fix does NOT reach: in production `integrate_note` and
`emr_parse` both fan out of one `note.ingested` with no ordering between them, and each
ends in a whole-note settle, so a real import still loses ALL of one producer's facts or
none. That collision is older and wider than this module (it is the shipped `apply_intent`
on both sides) and is recorded as open in the plan's W4 section; what is fixed here is the
importer destroying its OWN earlier attachments.

The EMR facts are all surface-attested (deterministic parse), so every fact gets
`surface_attested=True`; `fhir_status` — not `correction` — drives the lab
lifecycle transition inside `decide`, so `is_supersede` stays False.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.analysis.arbiter import plan_intent
from jbrain.analysis.extraction import Extraction
from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.emr.candidates import ParseResult
from jbrain.ingest.emr.firewall import FIREWALL_REVIEW_KIND
from jbrain.ingest.emr.importer import ChunkResolver, FirewallCatch, lower_parse_result
from jbrain.ingest.emr.pathology import extract_pathology_diagnoses
from jbrain.ingest.emr.reconcile import REVIEW_KIND, ParkedRead
from jbrain.models.analysis import ReviewItem

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jbrain.analysis.entities import ResolvedEntity

_SURFACE = ConfidenceSignals(surface_attested=True, is_supersede=False)
EXTRACTOR = "emr:deterministic"


async def file_parked_cards(
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    note_id: uuid.UUID,
    note_domain: str,
    parked: list[ParkedRead],
) -> int:
    """File a `low_confidence` card for each OCR read the reconciler parked (§6.4) —
    a readable-but-unmatched reprint is held for review, never minted as a fact. One
    card per (analyte, day) parked read; returns the number filed. Idempotent: a
    re-run re-derives the same (subkind, analyte, collected) key and files nothing.
    The probe spans ALL statuses, like the firewall card's below — an open-only probe
    treats a dismissal as a snooze and re-files the card on every `emr_parse` run,
    forever (the `wiki/lint._file_card` bug)."""
    if not parked:
        return 0
    filed = 0
    async with scoped_session(maker, ctx) as session:
        for p in parked:
            obs = p.observation
            key = f"{obs.analyte.code}|{obs.collected_at.isoformat()}"
            exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM app.review_items WHERE kind = :k"
                        " AND payload->>'subkind' = :sk AND payload->>'note_id' = :nid"
                        " AND payload->>'key' = :key LIMIT 1"
                    ),
                    {"k": REVIEW_KIND, "sk": p.subkind, "nid": str(note_id), "key": key},
                )
            ).first()
            if exists is not None:
                continue
            session.add(
                ReviewItem(
                    kind=REVIEW_KIND,
                    payload={
                        "note_id": str(note_id),
                        "subkind": p.subkind,
                        "key": key,
                        "analyte": obs.analyte.name,
                        "collected": obs.collected_at.isoformat(),
                        "source": obs.source_system,
                        "reason": p.reason,
                    },
                    domain_code=note_domain,
                )
            )
            filed += 1
    return filed


async def file_firewall_cards(
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    note_id: uuid.UUID,
    note_domain: str,
    catches: Sequence[tuple[str, FirewallCatch]],
) -> int:
    """File a `low_confidence` (`subkind=firewall_address`) card per Layer-2 firewall
    catch (§3.6) — the owner's only evidence that the guard fired and held a
    whereabouts fact out of the health graph. Each catch is paired with the id of the
    attachment it was caught in; returns the number filed.

    The card carries WHAT was held, WHERE (attachment + page anchor — durable across a
    re-ingest, which re-mints chunk rows) and HOW MANY times, and never the caught
    value: the value was kept out of the health domain on purpose, and this card sits
    in that same domain, so parking the value in its payload would re-plant the leak.

    Its one verb is `dismiss` — a facility address that is genuinely wanted is added
    deliberately as a location-domain `Place` sidecar (§3.6), never from here. It says
    so with an explicit `choices` entry, because a card advertising none renders with
    NO buttons at all (frontend `payload.proposalsFor`), and it suppresses the footer's
    "correct it" (`correctable: false`): that composer files an `owner_correction` note
    in the card's own domain, so on THIS card it would prompt the owner to type the
    held address back into health, pinned at full weight.

    Deduped on (attachment, anchor, entity kind, predicate) across ALL statuses, so a
    dismissed card never nags again on a re-import. The anchor is page-granular and a
    page holds several encounters, so identical catches collapse into one card that
    reports its `count` rather than under-reporting the guard as having fired once.
    """
    if not catches:
        return 0
    filed = 0
    # One card per key, carrying how many catches collapsed into it: a control that
    # says "a fact was held" when it held four under-reports how often it fired.
    counts: dict[str, int] = {}
    first: dict[str, tuple[str, FirewallCatch]] = {}
    for attachment_id, catch in catches:
        key = f"{attachment_id}|{catch.anchor}|{catch.entity_kind}|{catch.predicate}"
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, (attachment_id, catch))
    async with scoped_session(maker, ctx) as session:
        for key, (attachment_id, catch) in first.items():
            exists = (
                await session.execute(
                    text(
                        "SELECT 1 FROM app.review_items WHERE kind = :k"
                        " AND payload->>'subkind' = :sk AND payload->>'note_id' = :nid"
                        " AND payload->>'key' = :key LIMIT 1"
                    ),
                    {
                        "k": FIREWALL_REVIEW_KIND,
                        "sk": catch.subkind,
                        "nid": str(note_id),
                        "key": key,
                    },
                )
            ).first()
            if exists is not None:
                continue
            held = counts[key]
            # No `snippet` (and no `statement`/`value_json`): `snippet` is what the
            # frontend's Evidence block renders as the card's cited source text, and
            # the source here is the page the address was read off — quoting it would
            # put the whereabouts back into the health domain the guard held it out of.
            session.add(
                ReviewItem(
                    kind=FIREWALL_REVIEW_KIND,
                    payload={
                        "note_id": str(note_id),
                        "subkind": catch.subkind,
                        "key": key,
                        "attachment_id": attachment_id,
                        "anchor": catch.anchor,
                        "predicate": catch.predicate,
                        "entity_kind": catch.entity_kind,
                        "count": held,
                        "summary": (
                            f"location firewall: {held} {catch.predicate} fact"
                            f"{'' if held == 1 else 's'} on a health {catch.entity_kind}"
                            f" held out of the graph at {catch.anchor}"
                        ),
                        "rationale": (
                            "the value is deliberately not recorded here. Whereabouts"
                            " that are genuinely needed are recorded as a location-domain"
                            " Place, never as a health fact."
                        ),
                        "choices": [
                            {
                                "action": "dismiss",
                                "label": "Dismiss",
                                "detail": "the held fact stays out of the health graph",
                            }
                        ],
                        "correctable": False,
                    },
                    domain_code=note_domain,
                )
            )
            filed += 1
    return filed


@dataclass
class EmrNoteCommit:
    """One note's EMR write, across every attachment it has (D9, plan constraint 6).

    Built once per `emr_parse` run, fed one parsed source at a time, and settled ONCE.
    The accumulation is the whole point: `settle_note` retracts every non-pinned fact
    of the note that is not in `touched`, so a settle per attachment retracts the
    attachments before it.

    Each source still commits in its OWN transaction — a worker crash re-runs only the
    unfinished ones, and an out-of-order commit can't FK-fault (encounter_id/part_of_id
    reference `app.entities`, written before any projection row within its own commit).
    What a crash now costs is the SETTLE, not the earlier attachments: the note keeps
    facts the sweep never reconciled and no `note_analysis` stamp, and the next
    `emr_parse` run (idempotent, and re-enqueued by the W1 rebuild sweep's
    `_EMR_REPARSE_SQL`) settles them. That is strictly better than the old shape, where
    the crash window left earlier attachments RETRACTED.
    """

    pipeline: AnalysisPipeline
    maker: async_sessionmaker
    ctx: SessionContext
    note_id: uuid.UUID
    note_domain: str
    captured_at: datetime
    title: str = "Medical records"
    tags: list[str] = field(default_factory=list)

    _touched: set[uuid.UUID] = field(default_factory=set, init=False)
    _projected: set[uuid.UUID] = field(default_factory=set, init=False)
    _mention_ids: set[uuid.UUID] = field(default_factory=set, init=False)
    _resolved: dict[str, ResolvedEntity | None] = field(default_factory=dict, init=False)
    _extractions: list[Extraction] = field(default_factory=list, init=False)
    # Every attachment's chunks, de-duplicated in first-seen order: the settle's alias
    # and truncation passes are whole-note, so handing them one attachment's pages would
    # make them read the note as if the others were not there.
    _chunks: list[_ChunkRef] = field(default_factory=list, init=False)
    _chunk_ids: set[uuid.UUID] = field(default_factory=set, init=False)
    committed: int = field(default=0, init=False)

    async def commit_source(
        self,
        *,
        chunks: list[_ChunkRef],
        result: ParseResult,
        chunk_for_anchor: ChunkResolver,
    ) -> list[FirewallCatch]:
        """Commit ONE parsed source's facts and remember what it wrote.

        Returns that source's Layer-2 firewall catches — facts held out of the graph
        before an intent ever existed. The caller MUST card them with
        `file_firewall_cards`, or a security control fires silently.
        """
        # The one LLM touch on the structured path (§6.5): the pathology Final
        # Diagnosis. Fail-soft — an unrouted task / unusable reply yields no diagnoses
        # and the deterministic labs + encounters still commit.
        diagnoses = await extract_pathology_diagnoses(
            self.pipeline._router, result.pathology_narrative or ""
        )
        # Layer 2 runs INSIDE `lower_parse_result`, on the way from a parser candidate
        # to an `IntentFact` — so a location-locked predicate on a health EMR entity
        # never becomes a fact any commit path could see. Nothing downstream of here can
        # un-hold one, which is what makes it a hard non-commit rather than a filter.
        intents, catches = lower_parse_result(
            result, str(self.note_id), chunk_for_anchor, pathology_diagnoses=diagnoses
        )
        for chunk in chunks:
            if chunk.id not in self._chunk_ids:
                self._chunk_ids.add(chunk.id)
                self._chunks.append(chunk)
        for intent in intents:
            signals = {i: _SURFACE for i in range(len(intent.facts))}
            plan = plan_intent(intent, signals=signals)
            async with scoped_session(self.maker, self.ctx) as session:
                applied = await self.pipeline.commit_intent(
                    session,
                    note_id=self.note_id,
                    note_domain=self.note_domain,
                    captured_at=self.captured_at,
                    chunks=chunks,
                    intent=intent,
                    plan=plan,
                    title=self.title,
                    tags=self.tags,
                    extractor=EXTRACTOR,
                )
            if applied is None:  # rejected plan — nothing written, nothing to settle
                continue
            self._touched |= applied.outcome.touched
            self._projected |= applied.outcome.projected
            self._mention_ids |= applied.outcome.mention_ids
            # `resolved` is a MAP, so a bare `|=` is LAST-WINS where its three siblings
            # above are unions — and the refs collide across sources by design: they are
            # semantic keys (`org:Quest`, `cond:E11.9`, `obs:<code>`) and `new`-mode
            # resolution mints a fresh provisional per intent, so one ref on two
            # attachments is two entities. The settle reads this map for
            # `_register_declared_aliases`, `_reproject_entities` and
            # `_promote_corroborated`, so a dropped entry is an entity whose facts the
            # sweep spared but whose projection and promotion never ran. Displaced
            # entities are re-filed under their own id: the two set-shaped consumers
            # dedupe on `entity.id` and see them, and the alias pass looks up BY REF —
            # never by a uuid — so the extra keys are inert there.
            for ref, entity in applied.outcome.resolved.items():
                prior = self._resolved.get(ref)
                if prior is not None and (entity is None or prior.id != entity.id):
                    self._resolved[f"{ref}#{prior.id}"] = prior
                self._resolved[ref] = entity
            self._extractions.append(applied.extraction)
            self.committed += len(applied.outcome.touched)
        return catches

    async def settle(self) -> bool:
        """Close the note out once every attachment has committed — the ONE whole-note
        settle (constraint 6).

        Refuses on an empty run and says so with its return value. That is not tidiness:
        `settle_note` with an empty `touched` retracts every non-pinned fact of the note,
        so settling a run that parsed nothing would delete the note's graph — including
        whatever the still-running `integrate_note` producer wrote beside it (D13).
        """
        if not self._extractions:
            return False
        union = Extraction(
            title=self.title,
            tags=list(self.tags),
            mentions=[m for e in self._extractions for m in e.mentions],
            facts=[f for e in self._extractions for f in e.facts],
            tokens=[t for e in self._extractions for t in e.tokens],
            dropped_facts=sum(e.dropped_facts for e in self._extractions),
        )
        async with scoped_session(self.maker, self.ctx) as session:
            await self.pipeline.settle_note(
                session,
                note_id=self.note_id,
                note_domain=self.note_domain,
                chunks=self._chunks,
                extraction=union,
                extractor=EXTRACTOR,
                resolved=self._resolved,
                touched=self._touched,
                projected=self._projected,
                mention_ids=self._mention_ids,
            )
        return True


async def integrate_parse_result(
    pipeline: AnalysisPipeline,
    maker: async_sessionmaker,
    ctx: SessionContext,
    *,
    note_id: uuid.UUID,
    note_domain: str,
    captured_at: datetime,
    chunks: list[_ChunkRef],
    result: ParseResult,
    chunk_for_anchor: ChunkResolver,
    title: str = "Medical records",
    tags: list[str] | None = None,
) -> list[FirewallCatch]:
    """Commit ONE parse result and settle its note — the single-source convenience
    wrapper over `EmrNoteCommit`, for callers (the tests, a one-PDF import) that have
    exactly one source. A caller with several sources on one note must drive
    `EmrNoteCommit` itself: calling this in a loop settles per source, which is the
    retraction constraint 6 describes.

    Returns the Layer-2 firewall catches — the caller MUST card them with
    `file_firewall_cards`, or a security control fires silently. Provider resolution
    runs under `ctx` — pass a health-only scope so a general-domain namesake is
    invisible and a health `Person` is minted, not re-matched (§3.6)."""
    run = EmrNoteCommit(
        pipeline,
        maker,
        ctx,
        note_id=note_id,
        note_domain=note_domain,
        captured_at=captured_at,
        title=title,
        tags=tags or [],
    )
    catches = await run.commit_source(
        chunks=chunks, result=result, chunk_for_anchor=chunk_for_anchor
    )
    await run.settle()
    return catches
