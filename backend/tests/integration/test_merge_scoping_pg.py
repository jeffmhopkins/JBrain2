"""A cross-domain fold is all-or-nothing, against real Postgres.

`app.facts` carries its own `domain_code`, so a `general` entity routinely owns
`health` facts. Under a domain-narrowed session (`owner_scoped='true'`) RLS filters
the fold's repoint UPDATEs to the rows the session can see, which would tombstone an
entity and move only some of its facts — and `RETURNING` cannot detect the leftovers,
because it too returns only visible rows. So the fold and the un-fold refuse a
narrowed session outright, before writing anything.

The trigger (migration 0187) is defence in depth, NOT the guarantee, and this module
pins both halves of that: it refuses a raw-SQL tombstone on a row the session can
see, and it is provably BLIND when the loser row is itself out of scope — the
statement matches nothing, so no row trigger fires. Only the Python guard covers
that shape, which is why the two are tested as separate claims.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.entities import MergeScopeError
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_review_reopen_pg import (  # noqa: F401
    maker,
    one_row,
    seed_entity,
    seed_fact,
    seed_item,
    seed_note,
)
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The narrowed agent session the ingest plan puts a note conversation on: owner
# identity (owner-only tables stay readable) firewalled to one domain.
NARROWED = SessionContext(
    principal_id=OWNER.principal_id,
    principal_kind="owner",
    domain_scopes=("general",),
    owner_scoped=True,
)


async def _subject_of(maker: async_sessionmaker[AsyncSession], fact_id: str) -> str:  # noqa: F811
    """A fact's subject entity, read as a FULL owner — the only scope that sees both
    halves of a cross-domain pair, which is precisely why a narrowed session cannot
    audit its own fold."""
    row = await one_row(maker, OWNER, "SELECT entity_id FROM app.facts WHERE id = :id", id=fact_id)
    return str(row.entity_id)


async def _cross_domain_pair(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> tuple[str, str, str, str]:
    """Two `general` entities where the loser owns one visible and one firewalled
    fact — the shape that half-merges."""
    keep = await seed_entity(maker, f"Dr Patel {uuid.uuid4().hex[:6]}")
    gone = await seed_entity(maker, f"Patel {uuid.uuid4().hex[:6]}")
    note = await seed_note(maker)
    seen = await seed_fact(maker, note, gone, predicate="worksFor", domain="general")
    hidden = await seed_fact(maker, note, gone, predicate="treats", domain="health")
    return keep, gone, seen, hidden


async def test_narrowed_merge_refuses_and_leaves_nothing_behind(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The defect this guards: a narrowed fold would tombstone `gone` and repoint
    only its in-scope fact. It must refuse instead, with zero partial effect."""
    keep, gone, seen, hidden = await _cross_domain_pair(maker)

    repo = SqlAnalysisRepo(maker)
    with pytest.raises(MergeScopeError):
        await repo.merge_entities(NARROWED, keep, gone)

    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "provisional" and entity.merged_into_id is None
    for fact_id in (seen, hidden):
        assert await _subject_of(maker, fact_id) == gone


async def test_full_owner_merge_moves_the_out_of_scope_facts_too(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The escalation the refusal pushes to the caller does complete the fold: on a
    full-owner session every fact repoints, firewalled domain included."""
    keep, gone, seen, hidden = await _cross_domain_pair(maker)

    outcome = await SqlAnalysisRepo(maker).merge_entities(OWNER, keep, gone)
    assert outcome.merged is True and outcome.gone_id == gone

    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "merged" and str(entity.merged_into_id) == keep
    for fact_id in (seen, hidden):
        assert await _subject_of(maker, fact_id) == keep


async def test_narrowed_review_accept_refuses_the_fold(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The review-inbox merge runs the same fold, so it fails closed the same way —
    and the card stays open, since the refusal rolls the whole resolution back."""
    keep, gone, seen, hidden = await _cross_domain_pair(maker)
    item = await seed_item(maker, "merge_proposal", {"entity_a": keep, "entity_b": gone})

    repo = SqlAnalysisRepo(maker)
    with pytest.raises(MergeScopeError):
        await repo.resolve_review(NARROWED, item, "accept", {})

    card = await one_row(
        maker, OWNER, "SELECT status FROM app.review_items WHERE id = :id", id=item
    )
    assert card.status == "open"
    entity = await one_row(maker, OWNER, "SELECT status FROM app.entities WHERE id = :id", id=gone)
    assert entity.status == "provisional"
    for fact_id in (seen, hidden):
        assert await _subject_of(maker, fact_id) == gone


async def test_narrowed_reopen_refuses_the_un_merge_whole(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """Un-merge is the fold backwards and half-unwinds the same way, so a narrowed
    reopen must refuse before reversing ANY effect — the merge stays intact."""
    keep, gone, seen, hidden = await _cross_domain_pair(maker)
    item = await seed_item(maker, "merge_proposal", {"entity_a": keep, "entity_b": gone})

    repo = SqlAnalysisRepo(maker)
    resolved = await repo.resolve_review(OWNER, item, "accept", {})
    assert resolved is not None

    with pytest.raises(MergeScopeError):
        await repo.reopen_review(NARROWED, item)

    card = await one_row(
        maker, OWNER, "SELECT status FROM app.review_items WHERE id = :id", id=item
    )
    assert card.status == "resolved"
    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "merged" and str(entity.merged_into_id) == keep
    for fact_id in (seen, hidden):
        assert await _subject_of(maker, fact_id) == keep


async def test_full_owner_reopen_still_un_merges_everything(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The guard is a scope check, not a new restriction on un-merge: a full-owner
    reopen still moves every repointed row back, firewalled fact included."""
    keep, gone, seen, hidden = await _cross_domain_pair(maker)
    item = await seed_item(maker, "merge_proposal", {"entity_a": keep, "entity_b": gone})

    repo = SqlAnalysisRepo(maker)
    assert await repo.resolve_review(OWNER, item, "accept", {}) is not None
    reopened = await repo.reopen_review(OWNER, item)
    assert reopened is not None and reopened["status"] == "open"

    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "provisional" and entity.merged_into_id is None
    for fact_id in (seen, hidden):
        assert await _subject_of(maker, fact_id) == gone


async def test_the_table_refuses_a_narrowed_tombstone_written_in_raw_sql(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """RLS isolation for migration 0187: the Python guard is not the only lock. A
    narrowed session that reaches the tombstone with its own SQL — the entity row is
    in scope, so RLS alone would allow it — is refused by the trigger."""
    keep, gone, _, _ = await _cross_domain_pair(maker)

    with pytest.raises(DBAPIError) as caught:
        async with scoped_session(maker, NARROWED) as session:
            await session.execute(
                text(
                    "UPDATE app.entities SET status = 'merged', merged_into_id = :keep"
                    " WHERE id = :gone"
                ),
                {"keep": keep, "gone": gone},
            )
    assert "full-owner session" in str(caught.value)

    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "provisional" and entity.merged_into_id is None


async def test_the_table_refuses_a_narrowed_un_tombstone_written_in_raw_sql(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The trigger is symmetric: clearing the tombstone is an un-merge and needs the
    same scope, so a narrowed session cannot resurrect a merged-away entity either."""
    keep, gone, _, _ = await _cross_domain_pair(maker)
    await SqlAnalysisRepo(maker).merge_entities(OWNER, keep, gone)

    with pytest.raises(DBAPIError) as caught:
        async with scoped_session(maker, NARROWED) as session:
            await session.execute(
                text(
                    "UPDATE app.entities SET status = 'provisional', merged_into_id = NULL"
                    " WHERE id = :gone"
                ),
                {"gone": gone},
            )
    assert "full-owner session" in str(caught.value)

    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "merged" and str(entity.merged_into_id) == keep


async def test_a_narrowed_session_may_still_confirm_an_entity(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The trigger gates the merge tombstone alone — the narrowed ingest pipeline's
    ordinary entity writes (status confirm, summary refresh) must keep working."""
    entity_id = await seed_entity(maker, f"Confirmable {uuid.uuid4().hex[:6]}")

    async with scoped_session(maker, NARROWED) as session:
        await session.execute(
            text("UPDATE app.entities SET status = 'confirmed', summary = 'ok' WHERE id = :id"),
            {"id": entity_id},
        )

    entity = await one_row(
        maker, OWNER, "SELECT status FROM app.entities WHERE id = :id", id=entity_id
    )
    assert entity.status == "confirmed"


async def _cross_scope_pair(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> tuple[str, str, str]:
    """The shape the TRIGGER cannot see: the survivor is `general`, the loser lives in
    `health`, and the loser owns a `general` fact. A `general`-narrowed session can
    see the fact but not the entity."""
    keep = await seed_entity(maker, f"Keep {uuid.uuid4().hex[:6]}")
    gone = await seed_entity(maker, f"Gone {uuid.uuid4().hex[:6]}", domain="health")
    note = await seed_note(maker)
    fact = await seed_fact(maker, note, gone, predicate="worksFor", domain="general")
    return keep, gone, fact


async def test_the_trigger_is_blind_when_the_loser_row_is_out_of_scope(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The documented LIMIT of migration 0187, pinned so the claim cannot drift back.

    A `BEFORE ... FOR EACH ROW` trigger fires only for rows the statement matched, and
    RLS filters the scan first. With the loser in `health` and the session narrowed to
    `general`, the tombstone UPDATE matches zero rows and raises NOTHING, while the
    repoint still moves the fact it can see — the half-merge, with the table silent.
    This is why `require_unnarrowed_session` is the guarantee and the trigger is not.
    """
    keep, gone, fact = await _cross_scope_pair(maker)

    async with scoped_session(maker, NARROWED) as session:
        tombstone = await session.execute(
            text(
                "UPDATE app.entities SET status = 'merged', merged_into_id = :keep"
                " WHERE id = :gone RETURNING id"
            ),
            {"keep": keep, "gone": gone},
        )
        # No exception, and no rows: the trigger never got a row to inspect.
        assert tombstone.all() == []
        moved = await session.execute(
            text("UPDATE app.facts SET entity_id = :keep WHERE entity_id = :gone RETURNING id"),
            {"keep": keep, "gone": gone},
        )
        assert len(moved.all()) == 1  # ... and the repoint went through regardless

    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "provisional" and entity.merged_into_id is None
    assert await _subject_of(maker, fact) == keep  # the fact left its untombstoned owner


async def test_the_python_guard_covers_the_shape_the_trigger_cannot(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The same out-of-scope-loser shape through the real review path. `_apply_resolution`
    reads entity_a/entity_b straight off the card and never scope-checks them (unlike
    `merge_entities`), so the session guard is the only thing standing here."""
    keep, gone, fact = await _cross_scope_pair(maker)
    item = await seed_item(maker, "merge_proposal", {"entity_a": keep, "entity_b": gone})

    with pytest.raises(MergeScopeError):
        await SqlAnalysisRepo(maker).resolve_review(NARROWED, item, "accept", {})

    card = await one_row(
        maker, OWNER, "SELECT status FROM app.review_items WHERE id = :id", id=item
    )
    assert card.status == "open"
    entity = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert entity.status == "provisional" and entity.merged_into_id is None
    assert await _subject_of(maker, fact) == gone


async def test_the_table_refuses_a_narrowed_insert_of_an_already_merged_row(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """UPDATE is not the only way to reach the tombstone: `app.entities` grants INSERT
    and its WITH CHECK is `has_domain_scope` alone, so a narrowed session could
    otherwise create a row born merged."""
    keep = await seed_entity(maker, f"Survivor {uuid.uuid4().hex[:6]}")

    with pytest.raises(DBAPIError) as caught:
        async with scoped_session(maker, NARROWED) as session:
            await session.execute(
                text(
                    "INSERT INTO app.entities"
                    " (id, kind, canonical_name, domain_code, status, merged_into_id)"
                    " VALUES (:id, 'Person', 'Born merged', 'general', 'merged', :keep)"
                ),
                {"id": str(uuid.uuid4()), "keep": keep},
            )
    assert "full-owner session" in str(caught.value)


async def test_a_narrowed_session_may_still_create_an_ordinary_entity(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The INSERT gate asks one question only — is this row born a tombstone? Ordinary
    entity creation is the narrowed ingest pipeline's bread and butter."""
    entity_id = str(uuid.uuid4())
    async with scoped_session(maker, NARROWED) as session:
        await session.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:id, 'Person', 'Perfectly ordinary', 'general')"
            ),
            {"id": entity_id},
        )

    entity = await one_row(
        maker, OWNER, "SELECT status FROM app.entities WHERE id = :id", id=entity_id
    )
    assert entity.status == "provisional"


async def _conflict_card(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> tuple[str, str, str]:
    """A `fact_conflict` card whose resolution records pin/retract effects — an
    in-scope resolution with nothing cross-domain about it."""
    entity_id = await seed_entity(maker, f"Conflicted {uuid.uuid4().hex[:6]}")
    note = await seed_note(maker)
    fact_a = await seed_fact(maker, note, entity_id, predicate="jobTitle")
    fact_b = await seed_fact(maker, note, entity_id, predicate="jobTitle")
    item = await seed_item(maker, "fact_conflict", {"fact_a": fact_a, "fact_b": fact_b})
    return item, fact_a, fact_b


async def test_a_narrowed_reopen_of_a_non_merge_resolution_still_works(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The un-merge guard must not over-reach. Reversing a pin or a retraction is an
    in-scope single-row write, so a narrowed reopen of a resolution with no merge in
    it goes through — flip the guard to unconditional and this test is what fails."""
    item, fact_a, fact_b = await _conflict_card(maker)

    repo = SqlAnalysisRepo(maker)
    assert await repo.resolve_review(OWNER, item, "accept_a", {}) is not None
    pinned = await one_row(maker, OWNER, "SELECT pinned FROM app.facts WHERE id = :id", id=fact_a)
    assert pinned.pinned is True

    reopened = await repo.reopen_review(NARROWED, item)
    assert reopened is not None and reopened["status"] == "open"
    restored = await one_row(
        maker, OWNER, "SELECT pinned, status FROM app.facts WHERE id = :id", id=fact_a
    )
    assert restored.pinned is False
    loser = await one_row(maker, OWNER, "SELECT status FROM app.facts WHERE id = :id", id=fact_b)
    assert loser.status == "active"


async def test_a_narrowed_batch_collects_the_merge_error_and_commits_the_rest(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The batch's contract is that a bad item is an error while the good ones still
    commit. The scope guard raises before issuing a statement, so it has not poisoned
    the transaction and is exactly as collectable as UnknownAction — one merge card
    must not take 200 good ones down with it."""
    good, fact_a, _ = await _conflict_card(maker)
    keep, gone, fact = await _cross_scope_pair(maker)
    bad = await seed_item(maker, "merge_proposal", {"entity_a": keep, "entity_b": gone})

    result = await SqlAnalysisRepo(maker).resolve_review_batch(
        NARROWED,
        [
            {"id": good, "action": "accept_a", "payload": {}},
            {"id": bad, "action": "accept", "payload": {}},
        ],
    )

    assert [item["id"] for item in result["items"]] == [good]
    assert [e["id"] for e in result["errors"]] == [bad]
    assert "full-owner session" in result["errors"][0]["detail"]

    committed = await one_row(
        maker, OWNER, "SELECT status FROM app.review_items WHERE id = :id", id=good
    )
    assert committed.status == "resolved"
    pinned = await one_row(maker, OWNER, "SELECT pinned FROM app.facts WHERE id = :id", id=fact_a)
    assert pinned.pinned is True

    untouched = await one_row(
        maker, OWNER, "SELECT status FROM app.review_items WHERE id = :id", id=bad
    )
    assert untouched.status == "open"
    assert await _subject_of(maker, fact) == gone
