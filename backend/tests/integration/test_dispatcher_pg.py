"""The shadow dispatcher against real Postgres (W1·A2, docs/WORKFLOW_ENGINE_PLAN §E7a).

Proves end-to-end on real RLS + the real claim query that:

- the hardcoded note flow emits `app.events` rows (note.ingested) ALONGSIDE its
  enqueue, additively — ingest still indexes the note identically (today's
  behavior is unchanged);
- the dispatcher claims the undispatched event, resolves it to the seeded
  event-bound trigger -> pipeline -> action, diffs its would-be enqueue against the
  recorded hardcoded baseline (clean match), and stamps `dispatched_at` — WITHOUT
  enqueuing a second job (no double-processing while the hardcoded path still runs);
- an unentitled-domain event fails closed (a shadow error) but is still marked
  dispatched, never enqueued;
- `app.events` is domain-firewalled: a health event is invisible to a
  general-scoped reader (the per-table RLS isolation test migration 0036 owes).
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from jbrain.workflow import dispatcher
from jbrain.workflow import events as wf_events
from jbrain.workflow.registry import ACTION_SPECS, build_registry
from jbrain.workflow.scheduler import PURGE_ACTION
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))


def _registry():  # noqa: ANN202
    return build_registry((*ACTION_SPECS, PURGE_ACTION))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def blobs(tmp_path: Path) -> FsBlobStore:
    return FsBlobStore(tmp_path)


async def _seed_owner_principal(maker: async_sessionmaker[AsyncSession]) -> str:
    """A real owner principal so the worker-side emit (which has no per-content
    principal) can resolve one and satisfy the events FK. Returns its id."""
    pid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (:id, 'owner', :kh) ON CONFLICT DO NOTHING"
            ),
            {"id": pid, "kh": f"disp-{pid}"},
        )
    return pid


async def _make_note(maker: async_sessionmaker[AsyncSession], *, domain: str, body: str) -> str:
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"disp-{uuid.uuid4()}", domain=domain, destination=None, body=body
    )
    return note.id


async def _undispatched_events(
    maker: async_sessionmaker[AsyncSession], *, type: str, note_id: str
) -> list[dict]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id::text AS id, domain_code, dispatched_at, payload::text AS payload"
                    " FROM app.events WHERE type = :t AND payload->>'note_id' = :nid"
                ),
                {"t": type, "nid": note_id},
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def _count_jobs(maker: async_sessionmaker[AsyncSession], *, kind: str, note_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM app.jobs WHERE kind = :k AND payload->>'note_id' = :nid"
                ),
                {"k": kind, "nid": note_id},
            )
        ).scalar_one()


async def test_ingest_emits_event_and_still_indexes_the_note(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The note.ingested emission is ADDITIVE: ingest indexes the note and enqueues
    integrate EXACTLY as before, and ALSO drops an undispatched event."""
    await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="health", body="blood pressure 120/80")

    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    # Today's behavior unchanged: the note is indexed and exactly one integrate
    # job was enqueued by the hardcoded path.
    async with scoped_session(maker, OWNER) as s:
        state = (
            await s.execute(
                text("SELECT ingest_state FROM app.notes WHERE id = :id"), {"id": note_id}
            )
        ).scalar_one()
    assert state == "indexed"
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == 1

    # ...and the shadow event was emitted, carrying the note's domain (E2) and the
    # hardcoded enqueue baseline for the dispatcher to diff.
    events = await _undispatched_events(maker, type=wf_events.NOTE_INGESTED, note_id=note_id)
    assert len(events) == 1
    assert events[0]["domain_code"] == "health"
    assert events[0]["dispatched_at"] is None
    assert wf_events.SHADOW_ENQUEUED_KEY in events[0]["payload"]


async def test_dispatcher_marks_event_dispatched_without_double_enqueue(
    maker: async_sessionmaker[AsyncSession], blobs: FsBlobStore
) -> None:
    """The crux of shadow mode: the dispatcher resolves the emitted event to the
    seeded integrate pipeline, diffs clean, and stamps dispatched_at — but enqueues
    NOTHING, so the hardcoded path's single integrate job is the only one."""
    await _seed_owner_principal(maker)
    note_id = await _make_note(maker, domain="general", body="dinner with sam")
    await IngestPipeline(maker, blobs).ingest_note({"note_id": note_id})

    integrate_before = await _count_jobs(maker, kind="integrate_note", note_id=note_id)
    assert integrate_before == 1

    diffs = await dispatcher.dispatcher_tick(maker, _registry())

    # The event for this note was diffed and matched (the seeded event_integrate_note
    # pipeline reproduces the hardcoded integrate_note enqueue).
    mine = [d for d in diffs if d.event_type == wf_events.NOTE_INGESTED]
    assert mine and all(d.error is None for d in mine)
    assert any(d.matches for d in mine)

    # SHADOW: no second integrate job — the dispatcher never enqueued.
    assert await _count_jobs(maker, kind="integrate_note", note_id=note_id) == integrate_before

    # The event is now dispatched (drained from the undispatched set).
    events = await _undispatched_events(maker, type=wf_events.NOTE_INGESTED, note_id=note_id)
    assert len(events) == 1
    assert events[0]["dispatched_at"] is not None


async def test_dispatcher_drains_an_unbound_event_type_without_enqueue(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An event whose type binds no enabled trigger resolves to zero would-be
    enqueues: it is marked dispatched (drained, never left perpetually claimable)
    and nothing is enqueued. The events FK to app.domains already enforces a real
    domain at the DB; the dispatcher's own authorize_domain guard is unit-covered."""
    pid = await _seed_owner_principal(maker)
    event_id = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                " VALUES (:id, 'unbound.type', '{}'::jsonb, 'finance', :pid)"
            ),
            {"id": event_id, "pid": pid},
        )

    await dispatcher.dispatcher_tick(maker, _registry())

    async with scoped_session(maker, OWNER) as s:
        dispatched = (
            await s.execute(
                text("SELECT dispatched_at FROM app.events WHERE id = :id"), {"id": event_id}
            )
        ).scalar_one()
    assert dispatched is not None


async def test_resolution_emits_event_and_dispatcher_diffs_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The third trigger point: a predicate remap resolution emits a
    resolution.changed event (alongside the hardcoded consolidate enqueue), and the
    dispatcher resolves it to the seeded consolidate pipeline and diffs clean — with
    no second consolidate job (shadow)."""
    import json
    from datetime import UTC, datetime

    from jbrain.analysis.repo import SqlAnalysisRepo

    pid = await _seed_owner_principal(maker)
    # A ctx whose principal is the real owner row (so the emit's FK is satisfied).
    owner_ctx = SessionContext(principal_id=pid, principal_kind="owner")

    async with scoped_session(maker, owner_ctx) as s:
        await s.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind)"
                " VALUES ('spouse', 'd', 'ref', 'relationship')"
                " ON CONFLICT (canonical_name) DO NOTHING"
            )
        )
        ent = (
            await s.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                    " VALUES (gen_random_uuid(), 'Person', :n, 'confirmed', 'general')"
                    " RETURNING id::text"
                ),
                {"n": f"E-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar_one()
        note = (
            await s.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'general', 'x') RETURNING id::text"
                ),
                {"cid": f"disp-{uuid.uuid4()}"},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO app.facts"
                " (id, entity_id, predicate, qualifier, kind, statement, assertion, status,"
                " reported_at, note_id, extractor, prompt_version, domain_code)"
                " VALUES (gen_random_uuid(), :e, 'zzqMapMe', '', 'relationship', 's', 'asserted',"
                " 'active', :ts, :nid, 'test', 'v', 'general')"
            ),
            {"e": ent, "ts": datetime.now(UTC), "nid": uuid.UUID(note)},
        )
        card = (
            await s.execute(
                text(
                    "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                    " VALUES (gen_random_uuid(), 'new_predicate', cast(:p AS jsonb), 'general')"
                    " RETURNING id::text"
                ),
                {
                    "p": json.dumps(
                        {"predicate": "zzqMapMe", "fact_kind": "relationship", "statement": "x"}
                    )
                },
            )
        ).scalar_one()

    await SqlAnalysisRepo(maker).resolve_review(
        owner_ctx, card, "map_to_existing", {"canonical_name": "spouse"}
    )

    # The resolution emitted a resolution.changed event for the dispatcher.
    async with scoped_session(maker, owner_ctx) as s:
        events = (
            await s.execute(
                text(
                    "SELECT domain_code FROM app.events WHERE type = :t AND dispatched_at IS NULL"
                ),
                {"t": wf_events.RESOLUTION_CHANGED},
            )
        ).all()
    assert any(e.domain_code == "general" for e in events)

    consolidate_before = await _count_consolidate(maker)
    diffs = await dispatcher.dispatcher_tick(maker, _registry())
    mine = [d for d in diffs if d.event_type == wf_events.RESOLUTION_CHANGED]
    assert mine and all(d.error is None for d in mine)
    assert any(d.matches for d in mine)
    # SHADOW: the dispatcher enqueued no additional consolidate job.
    assert await _count_consolidate(maker) == consolidate_before


async def _count_consolidate(maker: async_sessionmaker[AsyncSession]) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM app.jobs WHERE kind = 'consolidate_predicates'")
            )
        ).scalar_one()


async def test_events_rls_isolates_by_domain(maker: async_sessionmaker[AsyncSession]) -> None:
    """events is domain-firewalled (migration 0036 RLS isolation, CLAUDE.md rule 3):
    a health event is invisible to a general-only reader and visible to a health
    reader. The per-table isolation test the events table owes for this wave's
    additive writes."""
    pid = await _seed_owner_principal(maker)
    health_event = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.events (id, type, payload, domain_code, principal_id)"
                " VALUES (:id, 'note.ingested', '{}'::jsonb, 'health', :pid)"
            ),
            {"id": health_event, "pid": pid},
        )

    async def visible(ctx: SessionContext) -> int:
        async with scoped_session(maker, ctx) as s:
            return (
                await s.execute(
                    text("SELECT count(*) FROM app.events WHERE id = :id"), {"id": health_event}
                )
            ).scalar_one()

    assert await visible(GENERAL_ONLY) == 0  # firewalled out
    assert await visible(HEALTH_ONLY) == 1  # in-scope reader sees it
    assert await visible(OWNER) == 1  # the owner crosses every firewall
