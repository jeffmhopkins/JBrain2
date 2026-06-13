"""SqlAnalysisRepo.merge_entities — the fold an owner-approved agent merge proposal
runs, against real Postgres. The agent path reuses the same merge as the review
inbox, so a merge proposal actually *combines* the entities (the duplicate is
tombstoned and its facts repoint), is idempotent on a re-enact, picks the survivor
by the trusted ranking, and a permanent distinct_from blocks it."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.analysis.repo import SqlAnalysisRepo, UnknownAction
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_review_reopen_pg import (  # noqa: F401
    maker,
    one_row,
    seed_entity,
    seed_fact,
    seed_note,
)
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


async def test_merge_entities_folds_the_duplicate_and_repoints_facts(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """The duplicate is tombstoned onto the survivor and its facts repoint — so
    after enact there is one canonical entity, not two joined by a same-as edge
    (the bug behind "I thought it would combine these, but it didn't")."""
    keep = await seed_entity(maker, "F-150")
    gone = await seed_entity(maker, "F150")
    note = await seed_note(maker)
    subj_fact = await seed_fact(maker, note, gone, predicate="manufacturer")
    obj_fact = await seed_fact(maker, note, keep, predicate="owns", object_entity_id=gone)

    repo = SqlAnalysisRepo(maker)
    outcome = await repo.merge_entities(OWNER, keep, gone)
    assert outcome.merged is True
    assert {outcome.keep_id, outcome.gone_id} == {keep, gone}

    survivor, tombstone = outcome.keep_id, outcome.gone_id
    gone_row = await one_row(
        maker, OWNER, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=tombstone
    )
    assert gone_row.status == "merged" and str(gone_row.merged_into_id) == survivor
    keep_row = await one_row(
        maker, OWNER, "SELECT status FROM app.entities WHERE id = :id", id=survivor
    )
    assert keep_row.status != "merged"  # the survivor is untouched

    # Facts that pointed at the tombstone (as subject or object) now point at the
    # survivor — none are stranded on the merged-away id.
    subj = await one_row(
        maker, OWNER, "SELECT entity_id FROM app.facts WHERE id = :id", id=subj_fact
    )
    obj = await one_row(
        maker, OWNER, "SELECT object_entity_id FROM app.facts WHERE id = :id", id=obj_fact
    )
    assert str(subj.entity_id) == survivor
    assert str(obj.object_entity_id) == survivor


async def test_merge_entities_is_idempotent_on_a_re_enact(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """A re-enacted proposal whose pair already merged is a no-op — never a second
    fold onto a tombstone."""
    keep = await seed_entity(maker, "Acme")
    gone = await seed_entity(maker, "Acme Inc")
    repo = SqlAnalysisRepo(maker)
    first = await repo.merge_entities(OWNER, keep, gone)
    assert first.merged is True
    again = await repo.merge_entities(OWNER, keep, gone)
    assert again.merged is False  # idempotent — nothing folded the second time


async def test_merge_entities_refuses_a_permanent_distinction(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
) -> None:
    """A rejected merge writes a permanent distinct_from; merge_entities must honour
    it and never fold the pair afterwards."""
    a = await seed_entity(maker, "Chase Visa")
    b = await seed_entity(maker, "Chase Sapphire")
    lo, hi = sorted((a, b))
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entity_distinctions (id, entity_a, entity_b) VALUES (:id, :a, :b)"
            ),
            {"id": str(uuid.uuid4()), "a": lo, "b": hi},
        )
    repo = SqlAnalysisRepo(maker)
    with pytest.raises(UnknownAction):
        await repo.merge_entities(OWNER, a, b)
