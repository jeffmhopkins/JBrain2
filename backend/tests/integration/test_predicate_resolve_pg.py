"""new_predicate card resolution (predicate canonicalization Phase 3b) against
real Postgres: accept_as_new mints the raw predicate; suggest_better mints under
a corrected name AND renames the committed fact onto it; map_to_existing rewrites
stored facts onto the canonical and emits a resolution.changed event that the
dispatcher resolves to the consolidation sweep (W2·C — no direct enqueue); reopen
reverses a map/rename but keeps a mint (durable vocabulary).
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import scoped_session
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import make_note, maker  # noqa: F401
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


async def _insert_card(maker, predicate: str, *, fact_kind: str = "relationship") -> str:  # noqa: F811
    payload = {
        "predicate": predicate,
        "fact_kind": fact_kind,
        "statement": f"x {predicate} y",
        "suggestions": [],
    }
    async with scoped_session(maker, OWNER) as session:
        return (
            await session.execute(
                text(
                    "INSERT INTO app.review_items (id, kind, payload, domain_code)"
                    " VALUES (gen_random_uuid(), 'new_predicate', cast(:p AS jsonb), 'general')"
                    " RETURNING id::text"
                ),
                {"p": json.dumps(payload)},
            )
        ).scalar_one()


async def _seed_canonical(maker, name: str) -> None:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind)"
                " VALUES (:n, 'd', 'ref', 'relationship') ON CONFLICT (canonical_name) DO NOTHING"
            ),
            {"n": name},
        )


async def _seed_fact(maker, predicate: str) -> tuple[str, str]:  # noqa: F811
    """An active fact under `predicate` on a fresh entity; returns (fact_id, note_id)."""
    note_id = await make_note(maker, domain="general", body="x")
    async with scoped_session(maker, SYSTEM_CTX) as session:
        ent = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                    " VALUES (gen_random_uuid(), 'Person', :n, 'confirmed', 'general')"
                    " RETURNING id::text"
                ),
                {"n": f"E-{predicate}"},
            )
        ).scalar_one()
        fact_id = (
            await session.execute(
                text(
                    "INSERT INTO app.facts"
                    " (id, entity_id, predicate, qualifier, kind, statement, assertion, status,"
                    " reported_at, note_id, extractor, prompt_version, domain_code)"
                    " VALUES (gen_random_uuid(), :e, :p, '', 'relationship', 's', 'asserted',"
                    " 'active', :ts, :nid, 'test', 'v', 'general') RETURNING id::text"
                ),
                {"e": ent, "p": predicate, "ts": datetime.now(UTC), "nid": uuid.UUID(note_id)},
            )
        ).scalar_one()
    return fact_id, note_id


async def _canonical(maker, name: str) -> dict | None:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT origin, value_shape, kind, embedding IS NULL AS no_emb"
                        " FROM app.canonical_predicates WHERE canonical_name = :n"
                    ),
                    {"n": name},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


async def _fact_predicate(maker, fact_id: str) -> str:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        return (
            await session.execute(
                text("SELECT predicate FROM app.facts WHERE id = :id"), {"id": fact_id}
            )
        ).scalar_one()


async def test_accept_as_new_mints_the_predicate(maker):  # noqa: F811
    card = await _insert_card(maker, "zzqNovelPred", fact_kind="state")
    resolved = await SqlAnalysisRepo(maker).resolve_review(OWNER, card, "accept_as_new", {})
    assert resolved is not None and resolved["status"] != "open"
    row = await _canonical(maker, "zzqNovelPred")
    assert row is not None
    assert row["origin"] == "minted" and row["value_shape"] == "scalar"
    assert row["kind"] == "state" and row["no_emb"]  # embedding left for the sync job


async def test_suggest_better_mints_and_renames_the_fact(maker):  # noqa: F811
    fact_id, _ = await _seed_fact(maker, "zzqRawForm")
    card = await _insert_card(maker, "zzqRawForm")
    await SqlAnalysisRepo(maker).resolve_review(
        OWNER, card, "suggest_better", {"canonical_name": "zzqBetterName"}
    )
    assert (await _canonical(maker, "zzqBetterName")) is not None
    assert (await _canonical(maker, "zzqRawForm")) is None  # minted under the better name only
    assert await _fact_predicate(maker, fact_id) == "zzqBetterName"  # the fact adopts the name


async def _seed_owner_principal(maker) -> str:  # noqa: F811
    """A real owner principal row so the resolution.changed emit satisfies the events
    FK; returns its id (the ctx principal the live tick will stamp the sweep with)."""
    pid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (:id, 'owner', :kh) ON CONFLICT DO NOTHING"
            ),
            {"id": pid, "kh": f"resolve-{pid}"},
        )
    return pid


async def _count_consolidate(maker) -> int:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        return (
            await session.execute(
                text("SELECT count(*) FROM app.jobs WHERE kind = 'consolidate_predicates'")
            )
        ).scalar_one()


async def test_map_to_existing_rewrites_facts_and_event_drives_consolidation(maker):  # noqa: F811
    # W2·C cutover: the resolution no longer enqueues consolidate directly — it emits
    # a resolution.changed event the dispatcher resolves to the consolidate pipeline.
    from jbrain.db.session import SessionContext
    from jbrain.workflow import dispatcher
    from jbrain.workflow import events as wf_events
    from jbrain.workflow.registry import ACTION_SPECS, build_registry
    from jbrain.workflow.runlog import PipelineRunLog
    from jbrain.workflow.scheduler import PURGE_ACTION

    pid = await _seed_owner_principal(maker)
    owner_ctx = SessionContext(principal_id=pid, principal_kind="owner")
    await _seed_canonical(maker, "spouse")
    fact_id, _ = await _seed_fact(maker, "zzqMapMe")
    card = await _insert_card(maker, "zzqMapMe")

    before = await _count_consolidate(maker)
    await SqlAnalysisRepo(maker).resolve_review(
        owner_ctx, card, "map_to_existing", {"canonical_name": "spouse"}
    )
    assert await _fact_predicate(maker, fact_id) == "spouse"  # healed in-band, as before

    # The resolution enqueued NO consolidate directly (the direct enqueue is gone)...
    assert await _count_consolidate(maker) == before
    # ...but it emitted an undispatched resolution.changed event.
    async with scoped_session(maker, owner_ctx) as s:
        emitted = (
            await s.execute(
                text("SELECT count(*) FROM app.events WHERE type = :t AND dispatched_at IS NULL"),
                {"t": wf_events.RESOLUTION_CHANGED},
            )
        ).scalar_one()
    assert emitted >= 1

    # A LIVE dispatcher tick resolves the event to the consolidate pipeline and
    # enqueues exactly one consolidate sweep.
    registry = build_registry((*ACTION_SPECS, PURGE_ACTION))
    diffs = await dispatcher.dispatcher_tick(
        maker, registry, live=True, run_log=PipelineRunLog(maker)
    )
    mine = [d for d in diffs if d.event_type == wf_events.RESOLUTION_CHANGED]
    assert mine and all(d.error is None for d in mine)
    assert await _count_consolidate(maker) == before + 1


async def test_map_to_unknown_canonical_is_rejected(maker):  # noqa: F811
    from jbrain.analysis.repo import UnknownAction

    card = await _insert_card(maker, "zzqNoTarget")
    with pytest.raises(UnknownAction):
        await SqlAnalysisRepo(maker).resolve_review(
            OWNER, card, "map_to_existing", {"canonical_name": "zzqNotACanonical"}
        )


async def test_reopen_reverses_a_map_but_keeps_a_mint(maker):  # noqa: F811
    repo = SqlAnalysisRepo(maker)
    # map -> reopen restores the raw predicate on the fact.
    await _seed_canonical(maker, "spouse")
    fact_id, _ = await _seed_fact(maker, "zzqReopenMap")
    map_card = await _insert_card(maker, "zzqReopenMap")
    await repo.resolve_review(OWNER, map_card, "map_to_existing", {"canonical_name": "spouse"})
    assert await _fact_predicate(maker, fact_id) == "spouse"
    await repo.reopen_review(OWNER, map_card)
    assert await _fact_predicate(maker, fact_id) == "zzqReopenMap"  # reversed

    # accept -> reopen keeps the minted predicate (durable vocabulary).
    accept_card = await _insert_card(maker, "zzqReopenMint")
    await repo.resolve_review(OWNER, accept_card, "accept_as_new", {})
    await repo.reopen_review(OWNER, accept_card)
    assert (await _canonical(maker, "zzqReopenMint")) is not None  # not un-minted

    # suggest_better -> reopen restores the raw predicate on the fact but keeps
    # the mint (it renames like a map and mints like an accept).
    sb_fact, _ = await _seed_fact(maker, "zzqReopenSuggest")
    sb_card = await _insert_card(maker, "zzqReopenSuggest")
    await repo.resolve_review(OWNER, sb_card, "suggest_better", {"canonical_name": "zzqSuggested"})
    assert await _fact_predicate(maker, sb_fact) == "zzqSuggested"
    await repo.reopen_review(OWNER, sb_card)
    assert await _fact_predicate(maker, sb_fact) == "zzqReopenSuggest"  # rename reversed
    assert (await _canonical(maker, "zzqSuggested")) is not None  # mint kept
