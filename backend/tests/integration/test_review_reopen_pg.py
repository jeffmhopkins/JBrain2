"""Reopen = full unwind, against real Postgres: every resolution kind
records effects sufficient to reverse it, and reopen_review restores the
prior graph state in the same transaction that re-queues the item — except
permanent distinct_from edges, which survive by doctrine (docs/reference/ANALYSIS.md
"Alias resolution & separation")."""

import json
import uuid
from collections.abc import AsyncIterator
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

from jbrain.analysis.repo import AlreadyOpen, SqlAnalysisRepo, UnknownAction
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def one_row(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, sql: str, **params: Any
) -> Any:
    async with scoped_session(maker, ctx) as session:
        return (await session.execute(text(sql), params)).one()


async def seed_note(maker: async_sessionmaker[AsyncSession]) -> str:
    nid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'general', 'reopen seed note')"
            ),
            {"id": nid, "cid": f"reopen-{nid[:13]}"},
        )
    return nid


async def seed_fact(
    maker: async_sessionmaker[AsyncSession],
    note_id: str,
    entity_id: str,
    *,
    predicate: str,
    status: str = "active",
    domain: str = "general",
    object_entity_id: str | None = None,
) -> str:
    fid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, statement, assertion,"
                " reported_at, status, note_id, object_entity_id, extractor, prompt_version,"
                " domain_code) VALUES (:id, :eid, :pred, 'state', 'seed statement', 'asserted',"
                " now(), :status, :nid, :oid, 'fake-model', 'v1', :domain)"
            ),
            {
                "id": fid,
                "eid": entity_id,
                "pred": predicate,
                "status": status,
                "nid": note_id,
                "oid": object_entity_id,
                "domain": domain,
            },
        )
    return fid


async def seed_entity(maker: async_sessionmaker[AsyncSession], name: str) -> str:
    eid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:id, 'Person', :name, 'general')"
            ),
            {"id": eid, "name": name},
        )
    return eid


async def seed_item(
    maker: async_sessionmaker[AsyncSession],
    kind: str,
    payload: dict[str, Any],
    *,
    domain: str = "general",
) -> str:
    iid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                " VALUES (:id, :kind, cast(:payload AS jsonb), :domain)"
            ),
            {"id": iid, "kind": kind, "payload": json.dumps(payload), "domain": domain},
        )
    return iid


async def test_collision_reopen_restores_fact_statuses(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """accept_a pins the winner and retracts the loser; reopen puts both
    facts back exactly as the collision left them (pending_review)."""
    repo = SqlAnalysisRepo(maker)
    note = await seed_note(maker)
    entity = await seed_entity(maker, "Reopen Collision Subject")
    fact_a = await seed_fact(maker, note, entity, predicate="birthDate", status="pending_review")
    fact_b = await seed_fact(maker, note, entity, predicate="birthDate", status="pending_review")
    item = await seed_item(maker, "attribute_collision", {"fact_a": fact_a, "fact_b": fact_b})

    resolved = await repo.resolve_review(OWNER, item, "accept_a", {})
    assert resolved is not None
    effects = resolved["resolution"]["effects"]
    assert [e["action"] for e in effects] == ["pinned", "retracted"]
    winner = await one_row(
        maker, OWNER, "SELECT status, pinned FROM app.facts WHERE id = :id", id=fact_a
    )
    assert (winner.status, winner.pinned) == ("active", True)

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None
    assert reopened["status"] == "open" and reopened["resolved_at"] is None
    assert reopened["reopen_note"] is None
    for fid in (fact_a, fact_b):
        row = await one_row(
            maker,
            OWNER,
            "SELECT status, pinned, superseded_by FROM app.facts WHERE id = :id",
            id=fid,
        )
        assert (row.status, row.pinned, row.superseded_by) == ("pending_review", False, None)

    with pytest.raises(AlreadyOpen):
        await repo.reopen_review(OWNER, item)


async def test_inference_accept_pins_held_fact_and_reopen_restores(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """accept on a low_confidence_inference card pins the held pending_review fact
    active (so it survives reprocessing); reopen restores it to pending_review."""
    repo = SqlAnalysisRepo(maker)
    note = await seed_note(maker)
    entity = await seed_entity(maker, "Held Inference Subject")
    fact = await seed_fact(maker, note, entity, predicate="industry", status="pending_review")
    item = await seed_item(maker, "low_confidence_inference", {"fact_id": fact})

    resolved = await repo.resolve_review(OWNER, item, "accept", {})
    assert resolved is not None
    assert [e["action"] for e in resolved["resolution"]["effects"]] == ["pinned"]
    row = await one_row(
        maker, OWNER, "SELECT status, pinned FROM app.facts WHERE id = :id", id=fact
    )
    assert (row.status, row.pinned) == ("active", True)

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None and reopened["status"] == "open"
    row = await one_row(
        maker,
        OWNER,
        "SELECT status, pinned, superseded_by FROM app.facts WHERE id = :id",
        id=fact,
    )
    assert (row.status, row.pinned, row.superseded_by) == ("pending_review", False, None)


async def test_inference_reject_retracts_held_fact_and_reopen_restores(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """reject retracts the held fact; reopen restores it to pending_review."""
    repo = SqlAnalysisRepo(maker)
    note = await seed_note(maker)
    entity = await seed_entity(maker, "Rejected Inference Subject")
    fact = await seed_fact(maker, note, entity, predicate="industry", status="pending_review")
    item = await seed_item(maker, "low_confidence_inference", {"fact_id": fact})

    resolved = await repo.resolve_review(OWNER, item, "reject", {})
    assert resolved is not None
    assert [e["action"] for e in resolved["resolution"]["effects"]] == ["retracted"]
    row = await one_row(maker, OWNER, "SELECT status FROM app.facts WHERE id = :id", id=fact)
    assert row.status == "retracted"

    await repo.reopen_review(OWNER, item)
    row = await one_row(maker, OWNER, "SELECT status FROM app.facts WHERE id = :id", id=fact)
    assert row.status == "pending_review"


async def test_inference_resolution_requires_a_fact_id(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    item = await seed_item(maker, "low_confidence_inference", {})  # no fact_id
    repo = SqlAnalysisRepo(maker)
    with pytest.raises(UnknownAction):
        await repo.resolve_review(OWNER, item, "accept", {})


async def test_merge_accept_reopen_restores_entities_and_mentions(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Un-merge replays the recorded repoint list backwards: the tombstone
    lifts and exactly the moved mentions/facts return to the merged entity —
    rows that pointed at the survivor all along stay put."""
    repo = SqlAnalysisRepo(maker)
    note = await seed_note(maker)
    keep = await seed_entity(maker, "Dr. Anita Patel (reopen)")
    gone = await seed_entity(maker, "Dr. Patel (reopen)")
    chunk = str(uuid.uuid4())
    mention = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:id, :nid, 'general', 'paragraph', 0, 'saw dr. patel')"
            ),
            {"id": chunk, "nid": note},
        )
        await s.execute(
            text(
                "INSERT INTO app.entity_mentions (id, entity_id, chunk_id, note_id,"
                " surface_text, char_start, char_end, link_method, domain_code)"
                " VALUES (:id, :eid, :cid, :nid, 'Dr. Patel', 4, 13, 'exact_alias', 'general')"
            ),
            {"id": mention, "eid": gone, "cid": chunk, "nid": note},
        )
    subject_fact = await seed_fact(maker, note, gone, predicate="medicalSpecialty")
    keeper_fact = await seed_fact(maker, note, keep, predicate="worksFor")
    object_fact = await seed_fact(maker, note, keep, predicate="colleagueOf", object_entity_id=gone)
    item = await seed_item(maker, "merge_proposal", {"entity_a": keep, "entity_b": gone})

    resolved = await repo.resolve_review(OWNER, item, "accept", {})
    assert resolved is not None
    (effect,) = resolved["resolution"]["effects"]
    assert effect["action"] == "merged" and effect["entity_id"] == gone
    assert effect["mention_ids"] == [mention]
    assert effect["fact_ids"] == [subject_fact]
    assert effect["object_fact_ids"] == [object_fact]
    merged = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert merged.status == "merged" and str(merged.merged_into_id) == keep

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None and reopened["status"] == "open"
    restored = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert restored.status == "provisional" and restored.merged_into_id is None
    back = await one_row(
        maker, OWNER, "SELECT entity_id FROM app.entity_mentions WHERE id = :id", id=mention
    )
    assert str(back.entity_id) == gone
    fact_owner = await one_row(
        maker, OWNER, "SELECT entity_id FROM app.facts WHERE id = :id", id=subject_fact
    )
    assert str(fact_owner.entity_id) == gone
    edge = await one_row(
        maker,
        OWNER,
        "SELECT entity_id, object_entity_id FROM app.facts WHERE id = :id",
        id=object_fact,
    )
    assert str(edge.entity_id) == keep and str(edge.object_entity_id) == gone
    untouched = await one_row(
        maker, OWNER, "SELECT entity_id FROM app.facts WHERE id = :id", id=keeper_fact
    )
    assert str(untouched.entity_id) == keep


async def test_merge_reject_reopen_keeps_permanent_distinction(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A reopened merge-rejection re-queues the item, but the distinct_from
    edge is permanent by spec — it survives, and the response says so."""
    repo = SqlAnalysisRepo(maker)
    entity_a = await seed_entity(maker, "Chase Visa (reopen)")
    entity_b = await seed_entity(maker, "Chase Sapphire (reopen)")
    a, b = sorted((entity_a, entity_b))
    item = await seed_item(maker, "merge_proposal", {"entity_a": entity_a, "entity_b": entity_b})

    resolved = await repo.resolve_review(OWNER, item, "reject", {})
    assert resolved is not None
    (effect,) = resolved["resolution"]["effects"]
    assert effect == {"action": "distinct_from", "a": a, "b": b, "inserted": True}

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None and reopened["status"] == "open"
    assert reopened["reopen_note"] is not None and "permanent" in reopened["reopen_note"]
    edge = await one_row(
        maker,
        OWNER,
        "SELECT count(*) AS n FROM app.entity_distinctions WHERE entity_a = :a AND entity_b = :b",
        a=a,
        b=b,
    )
    assert edge.n == 1


async def test_domain_promotion_reopen_restores_domain_and_pin(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAnalysisRepo(maker)
    note = await seed_note(maker)
    entity = await seed_entity(maker, "Dr. Akin (reopen)")
    # Owner session writes span domains, so the health->general move is legal.
    fact = await seed_fact(maker, note, entity, predicate="faxRequest", domain="health")
    item = await seed_item(
        maker,
        "domain_promotion",
        {"fact_id": fact, "proposed_domain": "general"},
        domain="health",
    )

    resolved = await repo.resolve_review(OWNER, item, "accept", {})
    assert resolved is not None
    (effect,) = resolved["resolution"]["effects"]
    assert effect["action"] == "domain_changed"
    assert effect["prior_domain"] == "health" and effect["prior_pinned"] is False
    moved = await one_row(
        maker, OWNER, "SELECT domain_code, pinned FROM app.facts WHERE id = :id", id=fact
    )
    assert (moved.domain_code, moved.pinned) == ("general", True)

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None and reopened["status"] == "open"
    restored = await one_row(
        maker, OWNER, "SELECT domain_code, pinned FROM app.facts WHERE id = :id", id=fact
    )
    assert (restored.domain_code, restored.pinned) == ("health", False)


async def test_dismissal_reopen_is_a_bare_requeue(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAnalysisRepo(maker)
    item = await seed_item(maker, "low_confidence", {"summary": "shaky extraction"})
    resolved = await repo.resolve_review(OWNER, item, "dismiss", {})
    assert resolved is not None
    assert resolved["status"] == "dismissed" and resolved["resolution"]["effects"] == []

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None
    assert reopened["status"] == "open" and reopened["reopen_note"] is None


async def test_extraction_truncated_reject_is_a_dismissal(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The fact-budget notice wrote no graph state, so its only advertised verb
    (reject) resolves as a bare dismissal — no effects, reopen is a re-queue."""
    repo = SqlAnalysisRepo(maker)
    item = await seed_item(
        maker, "extraction_truncated", {"summary": "this note hit its fact budget"}
    )
    resolved = await repo.resolve_review(OWNER, item, "reject", {})
    assert resolved is not None
    assert resolved["status"] == "dismissed" and resolved["resolution"]["effects"] == []


async def test_resolved_listing_orders_and_tombstones(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The resolved segment folds in dismissals and reopened tombstones,
    newest decision first; a reopened item shows in BOTH segments."""
    repo = SqlAnalysisRepo(maker)
    first = await seed_item(maker, "low_confidence", {"summary": "first decided"})
    second = await seed_item(maker, "low_confidence", {"summary": "second decided"})
    await repo.resolve_review(OWNER, first, "dismiss", {})
    await repo.resolve_review(OWNER, second, "dismiss", {})

    log = await repo.list_review(OWNER, "resolved")
    ours = [i for i in log if i["id"] in (first, second)]
    assert [i["id"] for i in ours] == [second, first]  # newest decision first
    assert all(i["status"] == "dismissed" for i in ours)

    await repo.reopen_review(OWNER, first)
    log_after = {i["id"]: i for i in await repo.list_review(OWNER, "resolved")}
    tomb = log_after[first]
    assert tomb["status"] == "open" and tomb["resolution"]["reopened_at"]
    open_ids = {i["id"] for i in await repo.list_review(OWNER, "open")}
    assert first in open_ids and second not in open_ids


async def test_defer_parks_then_undefer_is_a_clean_requeue(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Deferring moves an item to its own lane (out of open, not in the
    decided log); un-deferring returns it to pending with the resolution
    cleared — no tombstone, because it was never decided."""
    repo = SqlAnalysisRepo(maker)
    item = await seed_item(maker, "low_confidence", {"summary": "park me"})

    deferred = await repo.resolve_review(OWNER, item, "defer", {})
    assert deferred is not None
    assert deferred["status"] == "deferred" and deferred["resolution"]["effects"] == []

    deferred_ids = {i["id"] for i in await repo.list_review(OWNER, "deferred")}
    open_ids = {i["id"] for i in await repo.list_review(OWNER, "open")}
    decided_ids = {i["id"] for i in await repo.list_review(OWNER, "resolved")}
    assert item in deferred_ids and item not in open_ids and item not in decided_ids

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None
    assert reopened["status"] == "open" and reopened["resolution"] is None
    assert reopened["reopen_note"] is None
    # A clean re-queue leaves no tombstone in the decided log.
    assert item not in {i["id"] for i in await repo.list_review(OWNER, "resolved")}
    assert item in {i["id"] for i in await repo.list_review(OWNER, "open")}


async def test_discuss_tags_the_deferred_row_for_the_assistant(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """'discuss' parks like defer but records its own action, so the lane can
    tag the row as handed to the assistant."""
    repo = SqlAnalysisRepo(maker)
    item = await seed_item(maker, "ambiguous_mention", {"summary": "which project?"})

    parked = await repo.resolve_review(OWNER, item, "discuss", {})
    assert parked is not None
    assert parked["status"] == "deferred"
    assert parked["resolution"]["action"] == "discuss"


async def test_correct_links_a_note_and_reopen_keeps_it(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A correction resolves the item by linking the note that carries the
    human's fix (the graph change is the pipeline's, when it processes that
    note); reopen re-queues the item but keeps the note."""
    repo = SqlAnalysisRepo(maker)
    note = await seed_note(maker)
    item = await seed_item(maker, "fact_conflict", {"summary": "wrong reading"})

    resolved = await repo.resolve_review(OWNER, item, "correct", {"note_id": note})
    assert resolved is not None
    assert resolved["status"] == "resolved"
    (effect,) = resolved["resolution"]["effects"]
    assert effect == {"action": "corrected", "note_id": note}

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None and reopened["status"] == "open"
    note_text = reopened["reopen_note"]
    assert note_text is not None and "correction note stays" in note_text


async def test_correct_without_a_note_is_rejected(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAnalysisRepo(maker)
    item = await seed_item(maker, "fact_conflict", {"summary": "wrong reading"})
    with pytest.raises(UnknownAction):
        await repo.resolve_review(OWNER, item, "correct", {})


async def test_resolve_batch_commits_good_and_collects_bad(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The batch applies each decision with its own action: the valid ones
    commit, the unknown-action and not-found ones come back as errors."""
    repo = SqlAnalysisRepo(maker)
    a = await seed_item(maker, "low_confidence", {"summary": "good one"})
    b = await seed_item(maker, "low_confidence", {"summary": "bad action"})
    missing = str(uuid.uuid4())

    out = await repo.resolve_review_batch(
        OWNER,
        [
            {"id": a, "action": "defer", "payload": {}},
            {"id": b, "action": "accept_a", "payload": {}},  # invalid for low_confidence
            {"id": missing, "action": "dismiss", "payload": {}},
        ],
    )
    assert [i["id"] for i in out["items"]] == [a]
    assert out["items"][0]["status"] == "deferred"
    error_ids = {e["id"] for e in out["errors"]}
    assert error_ids == {b, missing}
    # The bad item stayed open — a failed batch entry rolls nothing forward.
    assert b in {i["id"] for i in await repo.list_review(OWNER, "open")}


async def test_confirm_entity_accept_confirms_and_reopen_restores(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """accept on a confirm_entity card flips the provisional entity to confirmed;
    reopen restores it to provisional via the entity_confirmed effect."""
    repo = SqlAnalysisRepo(maker)
    entity = await seed_entity(maker, "Contested Zane")  # provisional by default
    item = await seed_item(
        maker,
        "confirm_entity",
        {"entity_id": entity, "entity_name": "Zane", "entity_kind": "Person"},
    )

    resolved = await repo.resolve_review(OWNER, item, "accept", {})
    assert resolved is not None
    assert [e["action"] for e in resolved["resolution"]["effects"]] == ["entity_confirmed"]
    row = await one_row(maker, OWNER, "SELECT status FROM app.entities WHERE id = :id", id=entity)
    assert row.status == "confirmed"

    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None and reopened["status"] == "open"
    row = await one_row(maker, OWNER, "SELECT status FROM app.entities WHERE id = :id", id=entity)
    assert row.status == "provisional"  # reopen reverts the confirmation


async def test_confirm_entity_reject_leaves_provisional(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """reject on a confirm_entity card records no effect and leaves the entity
    provisional (purge-eligible)."""
    repo = SqlAnalysisRepo(maker)
    entity = await seed_entity(maker, "Rejected Zane")
    item = await seed_item(maker, "confirm_entity", {"entity_id": entity, "entity_name": "Zane"})

    resolved = await repo.resolve_review(OWNER, item, "reject", {})
    assert resolved is not None
    assert resolved["resolution"]["effects"] == []
    row = await one_row(maker, OWNER, "SELECT status FROM app.entities WHERE id = :id", id=entity)
    assert row.status == "provisional"
