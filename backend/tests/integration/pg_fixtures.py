"""The integration suite's shared Postgres fixtures and write-path drivers.

These lived in `test_extraction_pg.py` and `test_apply_intent_pg.py` — twenty files
imported `maker`/`make_note`/`ingest` out of the first and the intent builders out of the
second — and both of those files were the deleted `integrate_note` chain's own spec. R4
took the specs and kept the fixtures, here, where importing them is not a claim about
what the importer tests.

`analyzer` is the one that changed shape. It used to script `note.extract` + an
`integrate.note` intent and run `AnalysisPipeline.integrate_note`; that producer is gone,
so it now PARSES the same scripted extraction and commits it through the surviving
deterministic path (`plan_intent` -> `commit_intent` -> `settle_note`) under the
`analyzer` settle key. What it stands for in a test is unchanged and still real: a
deterministic, non-EMR producer writing a whole note in one pass — which is also the
stamp on every row the deleted producer left on the owner's box.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
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

from jbrain.analysis.arbiter import compute_signals, plan_intent
from jbrain.analysis.extraction import Extraction, parse_extraction
from jbrain.analysis.intent import (
    AttestedSpan,
    EntityResolution,
    IntegrationIntent,
    IntentFact,
    IntentTemporal,
)
from jbrain.analysis.pipeline import AnalysisPipeline, _ChunkRef, local_anchor
from jbrain.analysis.prompt import fact_cap
from jbrain.analysis.settle_owner import ANALYZER
from jbrain.analysis.weight import ConfidenceSignals
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.chunker import PARAGRAPH
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.models.notes import Chunk
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import FsBlobStore
from jbrain.usage import SqlUsageRecorder
from tests.integration.test_rls import OWNER, database_url  # noqa: F401


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def make_note(
    maker: async_sessionmaker[AsyncSession],
    *,
    domain: str,
    body: str,
    created_at: datetime | None = None,
    tz_offset: int | None = None,
) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER,
        client_id=f"ana-{uuid.uuid4()}",
        domain=domain,
        destination=None,
        body=body,
        created_at=created_at,
        tz_offset_minutes=tz_offset,
    )
    return note.id


async def ingest(maker: async_sessionmaker[AsyncSession], note_id: str, tmp_path: Any) -> None:
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})


async def reingest_a_rewritten_body(
    maker: async_sessionmaker[AsyncSession], note_id: str, tmp_path: Any, body: str
) -> None:
    """Re-ingest over a REWRITTEN body — what actually nulls `facts.chunk_id` now.

    An unchanged re-ingest no longer does: `ingest.carryover` keeps a rebuilt chunk's row
    when it comes back byte-identical, so a re-ingest stops destroying the note's entity
    mentions and its published wiki citations. The in-place re-anchor stays load-bearing
    for the cases carry-over declines, and a rewrite is the plainest of them.
    """
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET body = :b WHERE id = :n"), {"b": body, "n": note_id}
        )
    await ingest(maker, note_id, tmp_path)


async def _seed_owner_principal(maker: async_sessionmaker[AsyncSession]) -> None:
    """A real owner principal so ingest's worker-side note.ingested emit (which has
    no per-content principal) can resolve one — without it emit_event short-circuits
    with 'no owner principal' and never writes the event row this test asserts."""
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (gen_random_uuid(), 'owner', :kh) ON CONFLICT DO NOTHING"
            ),
            {"kh": f"ana-{uuid.uuid4()}"},
        )


async def _entity_id_by_name(
    maker: async_sessionmaker[AsyncSession], name: str, domain: str
) -> str | None:
    """The live id of the most recent non-retracted entity with this canonical
    name — how the default intent resolves an existing-mode reference (and
    name-stable dedup across notes). 'Me' is the owner (general domain)."""
    async with scoped_session(maker, SYSTEM_CTX) as s:
        return (
            await s.execute(
                text(
                    "SELECT id::text FROM app.entities"
                    " WHERE canonical_name = :n AND status <> 'retracted'"
                    "   AND (:d = 'general' OR domain_code = :d OR canonical_name = 'Me')"
                    " ORDER BY created_at DESC LIMIT 1"
                ),
                {"n": name, "d": domain},
            )
        ).scalar_one_or_none()


async def rows(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    sql: str,
    **params: Any,
) -> list[Any]:
    async with scoped_session(maker, ctx) as session:
        return list((await session.execute(text(sql), params)).all())


def extraction_payload(**overrides: Any) -> dict[str, Any]:
    """The scripted checkup-note extraction the fake model returns."""
    payload: dict[str, Any] = {
        "title": "Morning checkup with Dr. Patel",
        "tags": ["health", "checkup", "blood pressure"],
        "mentions": [
            {"name": "Me", "kind": "Person", "surface_text": "My"},
            {"name": "Dr. Patel", "kind": "Person", "surface_text": "Dr. Patel"},
        ],
        "facts": [
            {
                "predicate": "bloodPressure",
                "qualifier": "",
                "kind": "measurement",
                "statement": "Blood pressure was 118/76 this morning.",
                "value_json": {"systolic": 118, "diastolic": 76, "unit": "mmHg"},
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": {
                    "phrase": "this morning",
                    "resolved_start": "2026-06-10T08:00:00+00:00",
                    "resolved_end": None,
                    "precision": "day",
                },
                "domain": "health",
                "confidence": 0.95,
            },
            {
                "predicate": "address",
                "qualifier": "",
                "kind": "state",
                "statement": "Lives at 99 Pine Ave.",
                "value_json": {"street": "99 Pine Ave"},
                "assertion": "asserted",
                "entity_ref": "Me",
                "object_entity_ref": None,
                "temporal": {
                    "phrase": "last week",
                    "resolved_start": "2026-06-03T00:00:00+00:00",
                    "resolved_end": None,
                    "precision": "day",
                },
                "domain": "general",
                "confidence": 0.8,
            },
        ],
        "temporal_tokens": [
            {
                "phrase": "this morning",
                "kind": "point",
                "resolved_start": "2026-06-10T08:00:00+00:00",
                "resolved_end": None,
                "precision": "day",
                "rrule": None,
            },
            {
                "phrase": "last week",
                "kind": "point",
                "resolved_start": "2026-06-03T00:00:00+00:00",
                "resolved_end": None,
                "precision": "day",
                "rrule": None,
            },
        ],
    }
    payload.update(overrides)
    return payload


_SURFACE = ConfidenceSignals(surface_attested=True, is_supersede=False)


async def _load_chunks(maker, note_id: str) -> list[_ChunkRef]:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        rows = (
            await session.execute(
                select(Chunk.id, Chunk.text)
                .where(Chunk.note_id == note_id, Chunk.granularity == PARAGRAPH)
                .order_by(Chunk.seq)
            )
        ).all()
    return [_ChunkRef(id=r.id, text=r.text) for r in rows]


def _fact(entity_ref: str, **kw) -> IntentFact:
    base: dict[str, Any] = dict(
        predicate="industry",
        qualifier="",
        kind="attribute",
        statement="Globex is in tech",
        value_json=None,
        assertion="asserted",
        object_entity_ref=None,
        temporal=None,
        attested_span=AttestedSpan("c", "Globex"),
        self_confidence=0.95,
        inferred=False,
    )
    base.update(kw)
    return IntentFact(entity_ref=entity_ref, **base)


def _intent(note_id: str, resolutions, facts) -> IntegrationIntent:
    return IntegrationIntent(
        note_id=note_id,
        schema_version=1,
        prompt_version="v13",
        integrator_version="i1",
        entity_resolutions=resolutions,
        facts=facts,
    )


async def commit_and_settle(
    pipeline: AnalysisPipeline,
    session: AsyncSession,
    *,
    note_id: uuid.UUID,
    note_domain: str,
    captured_at: datetime,
    chunks: list[_ChunkRef],
    intent: IntegrationIntent,
    plan: Any,
    title: str,
    tags: list[str],
    extractor: str,
    settle_owner: str,
    dropped_facts: int = 0,
) -> dict[str, Any]:
    """Commit an arbiter-approved intent AND settle its note in one call — what
    `AnalysisPipeline.apply_intent` was before R4 deleted it with its only production
    caller. The two halves it composed are both still production code (`commit_intent`
    is the EMR importer's own seam), so the tests that drove a whole note through one
    call keep driving one, from here."""
    applied = await pipeline.commit_intent(
        session,
        note_id=note_id,
        note_domain=note_domain,
        captured_at=captured_at,
        chunks=chunks,
        intent=intent,
        plan=plan,
        title=title,
        tags=tags,
        extractor=extractor,
        settle_owner=settle_owner,
        dropped_facts=dropped_facts,
    )
    if applied is None:
        return {}
    await pipeline.settle_note(
        session,
        note_id=note_id,
        note_domain=note_domain,
        chunks=chunks,
        extraction=applied.extraction,
        extractor=extractor,
        settle_owner=settle_owner,
        resolved=applied.outcome.resolved,
        touched=applied.outcome.touched,
        projected=applied.outcome.projected,
        mention_ids=applied.outcome.mention_ids,
    )
    return applied.override


CHECKUP_BODY = "Saw Dr. Patel this morning. My BP was 118/76. We moved to 99 Pine Ave last week."

# What `analyzer` stamps on the rows it writes.
_EXTRACTOR = "test:analyzer"


async def default_intent(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    extraction: Extraction,
    domain: str,
    body: str,
) -> IntegrationIntent:
    """The intent a faithful producer would emit for a PARSED extraction: resolve each
    referenced name (existing when a live entity already carries it, else new) and commit
    each surface-attested fact.

    These tests decouple the note body from the scripted extraction, so a mention's
    surface_text may not appear in the body; the fact's attested surface then falls back
    to the body itself (always in the haystack) so the weight model treats it as attested
    — a commit-everything default. A test that wants a fact HELD builds its own intent
    (cross_subject / ambiguous / inferred)."""
    from jbrain.analysis.entities import get_or_create_me

    async with scoped_session(maker, SYSTEM_CTX) as s:
        await get_or_create_me(s)

    def attesting(surface: str | None) -> str:
        return surface if surface and surface in body else body

    kind_by_name = {m.name: m.kind for m in extraction.mentions}
    surface_by_name = {m.name: m.surface_text for m in extraction.mentions}
    body_surface = next(iter(surface_by_name.values()), body)

    refs: list[str] = []
    for m in extraction.mentions:
        if m.name not in refs:
            refs.append(m.name)
    for f in extraction.facts:
        for ref in (f.entity_ref, f.object_entity_ref):
            if ref and ref not in refs:
                refs.append(ref)

    resolutions: list[EntityResolution] = []
    for name in refs:
        existing = await _entity_id_by_name(maker, name, domain)
        resolutions.append(
            EntityResolution(
                mention_ref=name,
                mode="existing" if existing is not None else "new",
                proposed_entity_id=existing,
                new_kind=None if existing is not None else kind_by_name.get(name, "Thing"),
                new_name=None if existing is not None else name,
                # The mention's own surface rides the resolution so plan_to_extraction
                # reprojects it (else the mention_ref doubles as the surface_text).
                attested_span=AttestedSpan("", surface_by_name.get(name, name)),
            )
        )
    facts = [
        IntentFact(
            entity_ref=f.entity_ref,
            predicate=f.predicate,
            qualifier=f.qualifier,
            kind=f.kind,
            statement=f.statement,
            value_json=f.value_json,
            assertion=f.assertion,
            object_entity_ref=f.object_entity_ref,
            temporal=(
                None
                if f.temporal is None
                else IntentTemporal(
                    phrase=f.temporal.phrase,
                    resolved_start=f.temporal.resolved_start,
                    resolved_end=f.temporal.resolved_end,
                    precision=f.temporal.precision,
                )
            ),
            attested_span=AttestedSpan(
                "", attesting(surface_by_name.get(f.entity_ref, body_surface))
            ),
            self_confidence=f.confidence,
            inferred=False,
        )
        for f in extraction.facts
    ]
    return _intent(note_id, resolutions, facts)


class _DeterministicDriver:
    """Parse a scripted `note.extract` payload and write the whole note in one pass
    through the surviving deterministic path, stamped `analyzer`."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        responses: list[str],
        *,
        embedder: Any = None,
        embed_model: str = "",
        intent: IntegrationIntent | None = None,
    ):
        self._maker = maker
        self._responses = responses
        self._embedder = embedder
        self._embed_model = embed_model
        # A pre-built intent REPLACES the name-match default, for a test that needs the
        # arbiter to see something the default never emits — a cross-subject resolution,
        # an inferred fact. Its note_id is re-stamped per call, so a caller builds it once.
        self._intent = intent

    async def analyze_note(self, payload: dict[str, Any]) -> None:
        note_id = str(payload["note_id"])
        async with scoped_session(self._maker, SYSTEM_CTX) as s:
            row = (
                await s.execute(
                    text(
                        "SELECT domain_code, body, created_at, tz_offset_minutes"
                        " FROM app.notes WHERE id = :i AND deleted_at IS NULL"
                    ),
                    {"i": note_id},
                )
            ).one_or_none()
        # A missing or deleted note is a no-op, as every note handler is.
        if row is None or not self._responses:
            return
        anchor = local_anchor(row.created_at, row.tz_offset_minutes)
        extraction = parse_extraction(
            json.loads(self._responses[0]),
            anchor=anchor if row.tz_offset_minutes is not None else None,
            max_facts=fact_cap(row.body),
        )
        intent = (
            _intent(note_id, self._intent.entity_resolutions, self._intent.facts)
            if self._intent is not None
            else await default_intent(self._maker, note_id, extraction, row.domain_code, row.body)
        )
        chunks = await _load_chunks(self._maker, note_id)
        texts = [c.text for c in chunks] or [row.body]
        plan = plan_intent(intent, compute_signals(intent, texts))
        # NO settings store, deliberately: `value_shape_enforce` and the held-fact
        # predicate picker are both `self._settings is not None` gated, and the driver
        # this replaced had none either. Handing one in would silently turn shape
        # enforcement on under every caller of this fixture — which drops a `ref`-shaped
        # predicate's literal value and takes the fact with it.
        pipeline = AnalysisPipeline(
            self._maker,
            LlmRouter({"xai": FakeLlmClient([])}, {}, recorder=SqlUsageRecorder(self._maker)),
            embedder=self._embedder,
            embed_model=self._embed_model,
        )
        async with scoped_session(self._maker, SYSTEM_CTX) as session:
            applied = await pipeline.commit_intent(
                session,
                note_id=uuid.UUID(note_id),
                note_domain=row.domain_code,
                captured_at=row.created_at,
                chunks=chunks,
                intent=intent,
                plan=plan,
                title=extraction.title,
                tags=extraction.tags,
                extractor=_EXTRACTOR,
                settle_owner=ANALYZER,
                dropped_facts=extraction.dropped_facts,
            )
            if applied is None:
                return
            await pipeline.settle_note(
                session,
                note_id=uuid.UUID(note_id),
                note_domain=row.domain_code,
                chunks=chunks,
                extraction=applied.extraction,
                extractor=_EXTRACTOR,
                settle_owner=ANALYZER,
                resolved=applied.outcome.resolved,
                touched=applied.outcome.touched,
                projected=applied.outcome.projected,
                mention_ids=applied.outcome.mention_ids,
            )
            await session.execute(
                text("UPDATE app.notes SET integration_state = 'integrated' WHERE id = :i"),
                {"i": note_id},
            )


def analyzer(
    maker: async_sessionmaker[AsyncSession],
    responses: list[str],
    *,
    embedder: Any = None,
    embed_model: str = "",
    intent: IntegrationIntent | None = None,
) -> _DeterministicDriver:
    return _DeterministicDriver(
        maker, responses, embedder=embedder, embed_model=embed_model, intent=intent
    )
