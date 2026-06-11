"""Note deletion purges the derived graph, against real Postgres
(docs/ANALYSIS.md: the one sanctioned exception to "nothing is deleted").

Deleting a note must hard-delete its facts, mentions, temporal tokens,
review items (any status), and note_analysis row, drop provisional entities
no surviving note references, and repair the supersession chains it cuts —
while the note row itself stays soft-deleted and other notes' graphs stay
untouched.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

T0 = datetime(2024, 1, 1, tzinfo=UTC)
T1 = datetime(2025, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def repo(maker: async_sessionmaker[AsyncSession]) -> SqlNotesRepo:
    return SqlNotesRepo(maker)


async def fetch(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> list[Any]:
    async with scoped_session(maker, OWNER) as s:
        return list((await s.execute(text(sql), params)).all())


async def count(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> int:
    (row,) = await fetch(maker, sql, **params)
    return row[0]


async def seed_note(maker: async_sessionmaker[AsyncSession]) -> str:
    nid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'general', 'purge seed note')"
            ),
            {"id": nid, "cid": f"purge-{nid[:13]}"},
        )
    return nid


async def seed_entity(
    maker: async_sessionmaker[AsyncSession],
    name: str,
    *,
    status: str = "provisional",
    subject_id: str | None = None,
) -> str:
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, subject_id,"
                " domain_code) VALUES (:id, 'Person', :name, :status, :sid, 'general')"
            ),
            {"id": eid, "name": name, "status": status, "sid": subject_id},
        )
    return eid


async def seed_fact(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    entity_id: str,
    *,
    predicate: str = "homeLocation",
    status: str = "active",
    superseded_by: str | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    object_entity_id: str | None = None,
    temporal_token_id: str | None = None,
    derived_from_fact_id: str | None = None,
) -> str:
    fid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " valid_from, valid_to, reported_at, status, superseded_by, note_id,"
                " object_entity_id, temporal_token_id, derived_from_fact_id, extractor,"
                " prompt_version, domain_code)"
                " VALUES (:id, :eid, :pred, 'state', 'seed statement', 'asserted', :vf, :vt,"
                " now(), :status, :sup, :nid, :oid, :tok, :derived, 'fake-model', 'v1', 'general')"
            ),
            {
                "id": fid,
                "eid": entity_id,
                "pred": predicate,
                "vf": valid_from,
                "vt": valid_to,
                "status": status,
                "sup": superseded_by,
                "nid": note_id,
                "oid": object_entity_id,
                "tok": temporal_token_id,
                "derived": derived_from_fact_id,
            },
        )
    return fid


async def seed_graph_extras(
    maker: async_sessionmaker[AsyncSession], note_id: str, entity_id: str
) -> tuple[str, str]:
    """Chunk + mention + temporal token + alias + note_analysis for a note;
    returns (mention_id, token_id)."""
    chunk, mention, token = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:id, :nid, 'general', 'paragraph', 0, 'purge seed chunk')"
            ),
            {"id": chunk, "nid": note_id},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id,"
                " surface_text, char_start, char_end, link_method, domain_code)"
                " VALUES (:id, :eid, :cid, :nid, 'seed', 0, 4, 'exact_alias', 'general')"
            ),
            {"id": mention, "eid": entity_id, "cid": chunk, "nid": note_id},
        )
        await s.execute(
            text(
                "INSERT INTO app.temporal_tokens (id, note_id, surface_phrase, kind,"
                " resolved_start, temporal_precision, capture_anchor, domain_code)"
                " VALUES (:id, :nid, 'last year', 'point', :start, 'day', now(), 'general')"
            ),
            {"id": token, "nid": note_id, "start": T1},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_aliases (id, entity_id, alias, alias_norm, domain_code)"
                " VALUES (:id, :eid, 'Seedy', 'seedy', 'general')"
            ),
            {"id": str(uuid.uuid4()), "eid": entity_id},
        )
        await s.execute(
            text(
                "INSERT INTO app.note_analysis (note_id, title, domain_code)"
                " VALUES (:nid, 'Purge seed', 'general')"
            ),
            {"nid": note_id},
        )
    return mention, token


async def seed_item(
    maker: async_sessionmaker[AsyncSession],
    kind: str,
    payload: dict[str, Any],
    *,
    status: str = "open",
) -> str:
    iid = str(uuid.uuid4())
    resolution = (
        json.dumps({"action": "accept_a", "payload": {}, "effects": []})
        if status != "open"
        else None
    )
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, status, resolution,"
                " resolved_at, domain_code) VALUES (:id, :kind, cast(:payload AS jsonb),"
                " :status, cast(:resolution AS jsonb),"
                " CASE WHEN :status = 'open' THEN NULL ELSE now() END, 'general')"
            ),
            {
                "id": iid,
                "kind": kind,
                "payload": json.dumps(payload),
                "status": status,
                "resolution": resolution,
            },
        )
    return iid


async def test_delete_purges_all_derived_artifacts(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    """Facts, mentions, tokens, note_analysis, and review items — open AND
    resolved — all go; the note row stays, soft-deleted."""
    note = await seed_note(maker)
    entity = await seed_entity(maker, "Purge Subject", status="confirmed")
    _, token = await seed_graph_extras(maker, note, entity)
    fact = await seed_fact(maker, note, entity, temporal_token_id=token)
    open_item = await seed_item(maker, "fact_conflict", {"fact_b": fact, "note_id": note})
    resolved_item = await seed_item(
        maker, "ambiguous_mention", {"name": "Seedy", "note_id": note}, status="resolved"
    )

    assert await repo.delete_note(OWNER, note)

    (note_row,) = await fetch(maker, "SELECT deleted_at FROM app.notes WHERE id = :id", id=note)
    assert note_row.deleted_at is not None
    for table in ("facts", "entity_mentions", "temporal_tokens", "note_analysis", "chunks"):
        assert (
            await count(maker, f"SELECT count(*) FROM app.{table} WHERE note_id = :id", id=note)
            == 0
        )  # noqa: E501
    assert (
        await count(
            maker,
            "SELECT count(*) FROM app.review_items WHERE id IN (:a, :b)",
            a=open_item,
            b=resolved_item,
        )
        == 0
    )
    # Confirmed entities are never purged, even when only this note cited them.
    assert await count(maker, "SELECT count(*) FROM app.entities WHERE id = :id", id=entity) == 1


async def test_delete_purges_derived_inverse_and_repairs_survivor_chain(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    """A derived inverse shares its source's note_id, so the source-note delete
    purges it for free (Issue 2). A surviving derived shadow that was chained
    onto a doomed derived row re-repairs like any fact: deleting the source
    note removes both the source and its derived shadow, and a survivor in
    another note whose chain pointed through the doomed shadow restores."""
    note_a, note_b = await seed_note(maker), await seed_note(maker)
    entity = await seed_entity(maker, "Inverse Subject")
    other = await seed_entity(maker, "Inverse Object")

    # note_b: a primary source edge + its derived reciprocal (same note_id).
    source_b = await seed_fact(maker, note_b, entity, predicate="spouse", object_entity_id=other)
    shadow_b = await seed_fact(
        maker,
        note_b,
        other,
        predicate="spouse",
        object_entity_id=entity,
        derived_from_fact_id=source_b,
        valid_from=T1,
    )
    # note_a: an older derived shadow superseded by note_b's shadow.
    shadow_a = await seed_fact(
        maker,
        note_a,
        other,
        predicate="spouse",
        object_entity_id=entity,
        status="superseded",
        superseded_by=shadow_b,
        valid_from=T0,
        valid_to=T1,
    )

    assert await repo.delete_note(OWNER, note_b)

    # Both the source and its derived shadow vanished with the note.
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=source_b) == 0
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=shadow_b) == 0
    # The survivor's chain died inside the doomed set, so it is restored.
    (restored,) = await fetch(
        maker,
        "SELECT status, superseded_by, valid_to FROM app.facts WHERE id = :id",
        id=shadow_a,
    )
    assert (restored.status, restored.superseded_by, restored.valid_to) == ("active", None, None)


async def test_chain_repair_restores_superseded_survivor(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    """Note A's fact was superseded by note B's; deleting B restores A's
    fact to active and reopens the SCD-2 close B's valid_from imposed."""
    note_a, note_b = await seed_note(maker), await seed_note(maker)
    entity = await seed_entity(maker, "Chain Subject")
    fact_b = await seed_fact(maker, note_b, entity, valid_from=T1)
    fact_a = await seed_fact(
        maker,
        note_a,
        entity,
        status="superseded",
        superseded_by=fact_b,
        valid_from=T0,
        valid_to=T1,
    )

    assert await repo.delete_note(OWNER, note_b)

    (restored,) = await fetch(
        maker,
        "SELECT status, superseded_by, valid_to FROM app.facts WHERE id = :id",
        id=fact_a,
    )
    assert (restored.status, restored.superseded_by, restored.valid_to) == ("active", None, None)


async def test_chain_repair_repoints_through_doomed_middle_link(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    """A -> B -> C with B deleted: A re-attaches to the surviving C and stays
    superseded with its interval close intact."""
    note_a, note_b, note_c = (
        await seed_note(maker),
        await seed_note(maker),
        await seed_note(maker),
    )
    entity = await seed_entity(maker, "Deep Chain Subject")
    fact_c = await seed_fact(maker, note_c, entity, valid_from=T2)
    fact_b = await seed_fact(
        maker, note_b, entity, status="superseded", superseded_by=fact_c, valid_from=T1, valid_to=T2
    )
    fact_a = await seed_fact(
        maker, note_a, entity, status="superseded", superseded_by=fact_b, valid_from=T0, valid_to=T1
    )

    assert await repo.delete_note(OWNER, note_b)

    (repointed,) = await fetch(
        maker,
        "SELECT status, superseded_by, valid_to FROM app.facts WHERE id = :id",
        id=fact_a,
    )
    assert repointed.status == "superseded"
    assert str(repointed.superseded_by) == fact_c
    assert repointed.valid_to == T1


async def test_provisional_entity_cleanup(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    """Provisional entities only this note referenced vanish (mention-only
    and object-only alike, aliases cascading); an entity shared with a
    surviving note stays with its surviving facts; the subject-linked "Me"
    entity is sacrosanct."""
    note, other_note = await seed_note(maker), await seed_note(maker)
    solo = await seed_entity(maker, "Solo Provisional")
    obj_only = await seed_entity(maker, "Object Only Provisional")
    shared = await seed_entity(maker, "Shared Provisional")
    subject = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:id, 'Me', 'person')"),
            {"id": subject},
        )
    me = await seed_entity(maker, "Me", status="confirmed", subject_id=subject)

    await seed_graph_extras(maker, note, solo)
    await seed_fact(maker, note, solo, predicate="colleagueOf", object_entity_id=obj_only)
    await seed_fact(maker, note, shared)
    await seed_fact(maker, other_note, shared, predicate="worksFor")
    await seed_fact(maker, note, me, predicate="weight")

    assert await repo.delete_note(OWNER, note)

    gone = await count(
        maker, "SELECT count(*) FROM app.entities WHERE id IN (:a, :b)", a=solo, b=obj_only
    )
    assert gone == 0
    assert (
        await count(maker, "SELECT count(*) FROM app.entity_aliases WHERE entity_id = :id", id=solo)
        == 0
    )  # noqa: E501
    assert await count(maker, "SELECT count(*) FROM app.entities WHERE id = :id", id=shared) == 1
    assert (
        await count(maker, "SELECT count(*) FROM app.facts WHERE entity_id = :id", id=shared) == 1
    )
    assert await count(maker, "SELECT count(*) FROM app.entities WHERE id = :id", id=me) == 1


async def test_review_item_bridging_doomed_and_surviving_fact_is_deleted(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    """A conflict card pairing a doomed fact with a surviving one goes too —
    even resolved — because one side's evidence is gone and its labels quote
    the doomed fact; the surviving fact itself is untouched."""
    note_a, note_b = await seed_note(maker), await seed_note(maker)
    entity = await seed_entity(maker, "Bridge Subject", status="confirmed")
    survivor = await seed_fact(maker, note_a, entity, status="pending_review")
    doomed = await seed_fact(maker, note_b, entity, status="pending_review")
    item = await seed_item(
        maker,
        "fact_conflict",
        {"fact_a": survivor, "fact_b": doomed, "note_id": note_a},
        status="resolved",
    )

    assert await repo.delete_note(OWNER, note_b)

    assert await count(maker, "SELECT count(*) FROM app.review_items WHERE id = :id", id=item) == 0
    (kept,) = await fetch(maker, "SELECT status FROM app.facts WHERE id = :id", id=survivor)
    assert kept.status == "pending_review"


async def test_cross_note_token_citation_is_unhooked_not_fatal(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    """Tokens are per-note by construction, but a stray cross-note citation
    must not abort the purge: the FK is nulled and the token still goes."""
    note, other_note = await seed_note(maker), await seed_note(maker)
    entity = await seed_entity(maker, "Token Subject", status="confirmed")
    _, token = await seed_graph_extras(maker, note, entity)
    stray = await seed_fact(maker, other_note, entity, temporal_token_id=token)

    assert await repo.delete_note(OWNER, note)

    assert (
        await count(maker, "SELECT count(*) FROM app.temporal_tokens WHERE id = :id", id=token) == 0
    )  # noqa: E501
    (kept,) = await fetch(maker, "SELECT temporal_token_id FROM app.facts WHERE id = :id", id=stray)
    assert kept.temporal_token_id is None


async def test_deleting_never_analyzed_note_is_a_clean_noop(
    maker: async_sessionmaker[AsyncSession], repo: SqlNotesRepo
) -> None:
    note = await seed_note(maker)
    assert await repo.delete_note(OWNER, note)
    (row,) = await fetch(maker, "SELECT deleted_at FROM app.notes WHERE id = :id", id=note)
    assert row.deleted_at is not None
    # Already-deleted (or unknown) reads as not found, same as before.
    assert not await repo.delete_note(OWNER, note)


async def test_backfill_sweeps_preexisting_orphans(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Notes deleted BEFORE the cascade existed left orphaned artifacts —
    including resolved review history (the owner's field report). The worker
    startup sweep purges them; a second run is a no-op."""
    from jbrain.analysis.purge import backfill_deleted_note_artifacts

    note = await seed_note(maker)
    entity = await seed_entity(maker, "Orphan Subject", status="provisional")
    _, token = await seed_graph_extras(maker, note, entity)
    await seed_fact(maker, note, entity, temporal_token_id=token)
    stray = await seed_item(
        maker, "ambiguous_mention", {"name": "Stray", "note_id": note}, status="resolved"
    )
    # Simulate a pre-cascade deletion: soft-delete the note row directly,
    # bypassing repo.delete_note so the purge never ran.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET deleted_at = now() WHERE id = :id"), {"id": note}
        )

    assert await backfill_deleted_note_artifacts(maker) == 1

    for table in ("facts", "entity_mentions", "temporal_tokens", "note_analysis"):
        assert (
            await count(maker, f"SELECT count(*) FROM app.{table} WHERE note_id = :id", id=note)
            == 0
        )
    assert await count(maker, "SELECT count(*) FROM app.review_items WHERE id = :id", id=stray) == 0
    # Provisional entity with no surviving references goes too.
    assert await count(maker, "SELECT count(*) FROM app.entities WHERE id = :id", id=entity) == 0
    # Idempotent: the swept note no longer matches any candidate predicate.
    assert await backfill_deleted_note_artifacts(maker) == 0
