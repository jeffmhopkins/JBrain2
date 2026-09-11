"""The corpus entity-graph rebuild sweep against real Postgres (analysis/rebuild.py).

The sweep re-derives every note's graph while KEEPING the notes, so it is the privacy
purge's destructive half with three exemptions that must hold exactly:

- the facts a human verdict rests on survive — the pinned row, the chain it superseded,
  AND the retracted loser a settled review card names, which no supersession walk can
  reach (resolving a card writes no chain edge at all) — so a rebuild never
  re-litigates a decision the owner already made;
- resolved review history survives (only OPEN cards go) — and survives usefully, with
  the facts its payload points at;
- agent episodes survive (nothing re-derives them).

Plus the two properties that make it operable without a terminal: it is resumable from
its own cursor, and it chains into the three-job wiki repair once re-integration drains.
"""

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

from jbrain.analysis import rebuild
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_note_purge_pg import (
    seed_entity,
    seed_fact,
    seed_graph_extras,
    seed_item,
    seed_note,
)
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


OPEN_RUNS = "SELECT count(*) FROM app.graph_rebuild_runs WHERE status <> 'completed'"
ITEM_BY_ID = "SELECT count(*) FROM app.review_items WHERE id = :id"
QUEUED_JOBS = "SELECT count(*) FROM app.jobs WHERE kind = :kind AND status = 'queued'"


async def fetch(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> list[Any]:
    async with scoped_session(maker, OWNER) as s:
        return list((await s.execute(text(sql), params)).all())


async def count(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> int:
    (row,) = await fetch(maker, sql, **params)
    return int(row[0])


async def quiesce(maker: async_sessionmaker[AsyncSession]) -> None:
    """A shared test database carries other suites' rows; the sweep is corpus-wide, so
    start every case from an empty corpus and an empty queue."""
    async with scoped_session(maker, OWNER) as s:
        # No DELETE grant (a run row is audit history): close them instead.
        await s.execute(text("UPDATE app.graph_rebuild_runs SET status = 'completed'"))
        # No DELETE grant on jobs either: retire them instead, which is what the
        # sweep's own dedup and drain checks read (queued/running only).
        await s.execute(text("UPDATE app.jobs SET status = 'done'"))
        await s.execute(text("UPDATE app.notes SET ingest_state = 'pending'"))


async def indexed_note(maker: async_sessionmaker[AsyncSession]) -> str:
    """A live note the sweep will pick up: indexed and already integrated."""
    note = await seed_note(maker)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.notes SET ingest_state = 'indexed',"
                " integration_state = 'integrated', wiki_built = true WHERE id = :id"
            ),
            {"id": note},
        )
    return note


async def entity_of(maker: async_sessionmaker[AsyncSession], table: str, row_id: str) -> str:
    """The `entity_id` a mention or fact currently points at — what an un-merge moves."""
    (row,) = await fetch(
        maker, f"SELECT entity_id::text AS eid FROM {table} WHERE id = :id", id=row_id
    )
    return str(row.eid)


async def run_row(maker: async_sessionmaker[AsyncSession]) -> Any:
    (row,) = await fetch(
        maker,
        "SELECT status, notes_done, facts_purged, facts_kept, cursor_note_id,"
        " wiki_rebuild_job_id FROM app.graph_rebuild_runs ORDER BY started_at DESC LIMIT 1",
    )
    return row


async def test_rebuild_removes_artifacts_and_requeues_integration(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Rebuild Subject", status="confirmed")
    await seed_graph_extras(maker, note, entity)
    await seed_fact(maker, note, entity)

    await rebuild.rebuild_batch(maker, start=True)

    for table in ("facts", "entity_mentions", "temporal_tokens", "note_analysis"):
        assert (
            await count(maker, f"SELECT count(*) FROM app.{table} WHERE note_id = :id", id=note)
            == 0
        )
    (row,) = await fetch(
        maker,
        "SELECT integration_state, deleted_at FROM app.notes WHERE id = :id",
        id=note,
    )
    assert row.integration_state == "pending_integration"
    assert row.deleted_at is None
    # The wiki's dirty bit is `entities.wiki_built` — 0046's triggers flip it on the
    # purge's own fact/mention deletes, which is what re-dirties the article. (The sweep
    # writes no `notes.wiki_built`: that column has no reader anywhere.)
    (dirty,) = await fetch(maker, "SELECT wiki_built FROM app.entities WHERE id = :id", id=entity)
    assert dirty.wiki_built is False
    assert (
        await count(
            maker,
            # `note_converse` since R3: re-integration re-enqueues the note's graph
            # producer, and that is the conversation now (`queue`'s reconciler).
            "SELECT count(*) FROM app.jobs WHERE kind = 'note_converse'"
            " AND status = 'queued' AND payload->>'note_id' = :id",
            id=note,
        )
        == 1
    )


async def test_pinned_fact_and_its_superseded_chain_survive(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A pinned fact is the owner's own decision (D11's force-supersede + pin), and the
    rows it superseded carry that verdict. Both survive; an unrelated fact does not.

    Sparing the whole chain is what keeps the rebuild from re-litigating: the re-derived
    twin of a spared row matches it by value, so `decide()` refreshes in place instead of
    landing a fresh row beside the pin and filing a collision card per settled decision.
    """
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Pinned Subject", status="confirmed")
    pin = await seed_fact(maker, note, entity, pinned=True)
    superseded = await seed_fact(maker, note, entity, status="superseded", superseded_by=pin)
    ordinary = await seed_fact(maker, note, entity, predicate="worksFor")

    progress = await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=pin) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=superseded) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=ordinary) == 0
    # The spared row keeps its link to the pin — the shape the verdict lives in.
    (row,) = await fetch(
        maker, "SELECT superseded_by::text AS sup FROM app.facts WHERE id = :id", id=superseded
    )
    assert row.sup == pin
    assert progress.kept == 2
    assert progress.purged == 1


async def test_spared_fact_keeps_the_temporal_token_it_cites(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Purging a token a spared fact cites would either abort on the FK or strand the
    fact's validity anchor. `_upsert_tokens` is get-or-create, so keeping it also lets
    re-extraction reuse the row rather than duplicating it."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Token Subject", status="confirmed")
    _, token = await seed_graph_extras(maker, note, entity)
    pin = await seed_fact(maker, note, entity, temporal_token_id=token, pinned=True)

    await rebuild.rebuild_batch(maker, start=True)

    (row,) = await fetch(
        maker, "SELECT temporal_token_id::text AS tok FROM app.facts WHERE id = :id", id=pin
    )
    assert row.tok == token
    assert await count(maker, "SELECT count(*) FROM app.temporal_tokens WHERE id = :id", id=token)


async def test_resolved_review_history_survives_and_open_cards_go(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A rebuild is not a deletion promise, so it uses the re-extraction sweep's
    discipline: only OPEN items go. Resolved/dismissed items are HUMAN history — and
    keeping the CARD is worthless if its facts go: `review_items.payload` is jsonb with
    no FK, so a purged side leaves a dangling pointer that nothing errors on, and
    reopen/undo silently no-ops forever (`_reverse_effects` replays an UPDATE against a
    row that is gone). The facts a settled item names are spared with it."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Review Subject", status="confirmed")
    fact = await seed_fact(maker, note, entity)
    open_only = await seed_fact(maker, note, entity, predicate="worksFor")
    open_card = await seed_item(maker, "fact_conflict", {"fact_b": open_only, "note_id": note})
    resolved_card = await seed_item(
        maker, "fact_conflict", {"fact_b": fact, "note_id": note}, status="resolved"
    )
    resolved_note_card = await seed_item(
        maker, "ambiguous_mention", {"name": "Seedy", "note_id": note}, status="resolved"
    )

    await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, ITEM_BY_ID, id=open_card) == 0
    for kept in (resolved_card, resolved_note_card):
        assert await count(maker, ITEM_BY_ID, id=kept) == 1
    # The settled card's fact survives; the one only an OPEN card cited does not.
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=fact) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=open_only) == 0


async def test_the_retracted_loser_of_a_settled_card_survives_a_rebuild(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The loser is reachable by NO supersession walk: resolving a card writes no chain
    edge — it pins the winner (`superseded_by = NULL`) and marks the loser retracted
    (analysis/repo.py). A `superseded_by`-only spare set misses it, deletes it, and the
    re-derived value then lands beside the pinned winner and re-flags: one collision
    card per settled decision, corpus-wide. Both sides — and the winner's derived
    shadow, which the resolution cascades onto — must survive."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Verdict Subject", status="confirmed")
    winner = await seed_fact(maker, note, entity, pinned=True)
    loser = await seed_fact(maker, note, entity, status="retracted")
    shadow = await seed_fact(maker, note, entity, derived_from_fact_id=winner)
    unrelated = await seed_fact(maker, note, entity, predicate="worksFor")
    card = await seed_item(
        maker, "attribute_collision", {"fact_a": winner, "fact_b": loser}, status="resolved"
    )

    progress = await rebuild.rebuild_batch(maker, start=True)

    for kept in (winner, loser, shadow):
        assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=kept) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=unrelated) == 0
    assert await count(maker, ITEM_BY_ID, id=card) == 1
    assert progress.kept == 3
    assert progress.purged == 1


async def test_a_deferred_cards_facts_survive_a_rebuild(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`deferred` is a real persisted status (migration 0024) and the purge does not
    retire it — only OPEN cards go. So a parked card outlives the rebuild, and a spare
    set keyed on "resolved or dismissed" leaves both the facts it cites to be deleted
    under it. Un-parking then returns it to the open queue citing two rows that are
    gone. The spare set is the complement of what the purge deletes for exactly this
    reason; neither side of a parked decision is pinned, so nothing else reaches them."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Parked Subject", status="confirmed")
    side_a = await seed_fact(maker, note, entity, status="pending_review")
    side_b = await seed_fact(maker, note, entity, status="pending_review", predicate="worksFor")
    card = await seed_item(
        maker, "fact_conflict", {"fact_a": side_a, "fact_b": side_b}, status="deferred"
    )

    await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, ITEM_BY_ID, id=card) == 1
    for kept in (side_a, side_b):
        assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=kept) == 1


async def test_a_rejected_inference_card_keeps_the_fact_it_names_by_fact_id(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A `low_confidence_inference` card names its fact as `payload.fact_id`, not
    `fact_a`/`fact_b`, and its REJECT retracts the row without pinning it (repo.py). So
    the row sits in neither the pin walk nor a two-key payload lookup: a spare set built
    from the collision card's shape alone hard-deletes it while the card survives, and
    the reopen that would restore its prior status replays
    `UPDATE app.facts ... WHERE id = :id` against nothing, forever. Sparing is keyed on
    every payload key ANY kind names a fact by, so `fact_id` kinds are covered too."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Inference Subject", status="confirmed")
    rejected = await seed_fact(maker, note, entity, status="retracted")
    unrelated = await seed_fact(maker, note, entity, predicate="worksFor")
    card = await seed_item(
        maker, "low_confidence_inference", {"fact_id": rejected}, status="resolved"
    )

    progress = await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, ITEM_BY_ID, id=card) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=rejected) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=unrelated) == 0
    assert (progress.kept, progress.purged) == (1, 1)


async def test_an_inverse_proposal_keeps_the_fact_it_names_by_source_fact_id(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`inverse_proposal` is the one kind filed outside `decide()`'s `review_kind`: the
    cross-subject firewall arm of `_write_inverse` (pipeline.py) refuses to auto-write a
    reciprocal onto another subject's stream and proposes it instead, naming the PRIMARY
    fact it mirrors as `payload.source_fact_id` — a key neither `fact_a`/`fact_b` nor
    `fact_id`. A spare set missing it purges the source while the card survives, leaving
    a card whose only provenance pointer is dangling. `resolve_review` has no branch for
    the kind, so no replay breaks here as it does for `fact_id` — but the mapping this
    module rests on claims to be COMPLETE, and an incomplete one is trusted anyway."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Reciprocal Subject", status="confirmed")
    source = await seed_fact(maker, note, entity, predicate="reportsTo")
    unrelated = await seed_fact(maker, note, entity, predicate="worksFor")
    card = await seed_item(
        maker, "inverse_proposal", {"source_fact_id": source}, status="dismissed"
    )

    progress = await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, ITEM_BY_ID, id=card) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=source) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=unrelated) == 0
    assert (progress.kept, progress.purged) == (1, 1)


async def test_a_settled_merge_still_un_merges_after_a_rebuild(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A resolved `merge_proposal` names two ENTITIES in its payload and nothing else —
    the row ids the fold actually moved live in `resolution->'effects'`
    (`mention_ids`/`fact_ids`/`object_fact_ids`, recorded by `merge_entity_pair`), and a
    reopen moves exactly those ids back one UPDATE at a time (`_reverse_effects`).

    So a payload-only spare set spares nothing here: the card survives the rebuild, the
    entity tombstone survives, and the reopen restores the entity row from
    `prior_status`/`prior_merged_into` — which are values, not ids — while moving ZERO
    mentions and ZERO facts. A half un-merge, silent, and worse than a clean no-op.

    The reopen is exercised in the window the sweep actually leaves open: the notes are
    purged and queued for re-integration, and the drain can run for hours, so an owner
    reopening a card mid-sweep is the ordinary case, not a contrived one.
    """
    await quiesce(maker)
    note = await indexed_note(maker)
    keep = await seed_entity(maker, "Merge Survivor", status="confirmed")
    gone = await seed_entity(maker, "Merge Tombstone", status="confirmed")
    mention, _token = await seed_graph_extras(maker, note, gone)
    subject_fact = await seed_fact(maker, note, gone)
    object_fact = await seed_fact(maker, note, keep, predicate="knows", object_entity_id=gone)
    card = await seed_item(maker, "merge_proposal", {"entity_a": keep, "entity_b": gone})

    repo = SqlAnalysisRepo(maker)
    await repo.resolve_review(OWNER, card, "accept", {})
    assert await entity_of(maker, "app.entity_mentions", mention) == keep
    assert await entity_of(maker, "app.facts", subject_fact) == keep

    await rebuild.rebuild_batch(maker, start=True)

    # The rows the recorded effects will replay are still there to be replayed.
    assert await count(maker, "SELECT count(*) FROM app.entity_mentions WHERE id = :id", id=mention)
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=subject_fact)
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=object_fact)

    await repo.reopen_review(OWNER, card)

    assert await entity_of(maker, "app.entity_mentions", mention) == gone
    assert await entity_of(maker, "app.facts", subject_fact) == gone
    (row,) = await fetch(
        maker,
        "SELECT object_entity_id::text AS oid FROM app.facts WHERE id = :id",
        id=object_fact,
    )
    assert row.oid == gone
    (tombstone,) = await fetch(
        maker, "SELECT status, merged_into_id FROM app.entities WHERE id = :id", id=gone
    )
    assert (tombstone.status, tombstone.merged_into_id) == ("confirmed", None)


async def emr_note(maker: async_sessionmaker[AsyncSession], *, media_types: tuple[str, ...]) -> str:
    """A sweep-eligible health `Records` note carrying `media_types` — the shape
    migration 0122's stage-2 trigger matches on."""
    nid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, destination, body,"
                " ingest_state, integration_state)"
                " VALUES (:id, :cid, 'health', 'Records', 'emr seed note',"
                " 'indexed', 'integrated')"
            ),
            {"id": nid, "cid": f"emr-{nid[:13]}"},
        )
        for media_type in media_types:
            await s.execute(
                text(
                    "INSERT INTO app.attachments (id, note_id, domain_code, sha256,"
                    " filename, media_type, size_bytes)"
                    " VALUES (:id, :n, 'health', :sha, 'records', :mt, 10)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "n": nid,
                    "sha": uuid.uuid4().hex,
                    "mt": media_type,
                },
            )
    return nid


EMR_JOBS = (
    "SELECT count(*) FROM app.jobs WHERE kind = 'emr_parse' AND status = 'queued'"
    " AND payload->>'note_id' = :id"
)


async def test_rebuild_re_drives_the_emr_parse_for_a_records_note(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An EMR note's facts have TWO producers — the generic LLM extraction and the
    deterministic parsers — and only the first has a re-drive path. The purge takes both
    halves; re-queuing integration alone returns the note holding only the LLM's read of
    a medical record, silently, under a docstring that promises the whole graph. So the
    sweep re-enqueues stage 2 for every note that still matches its markers."""
    await quiesce(maker)
    note = await emr_note(maker, media_types=("application/pdf",))

    await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, EMR_JOBS, id=note) == 1


async def test_rebuild_leaves_the_emr_parse_alone_where_stage_two_would_not_fire(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The markers are read off the note, not guessed: a note still holding an ARCHIVE is
    pre-decryption (stage 1's shape, and its password was scrubbed at intake, so stage 1
    can never re-run either), and a general note is not an EMR note at all. Re-driving
    either would queue a parse with nothing to parse."""
    await quiesce(maker)
    encrypted = await emr_note(maker, media_types=("application/pdf", "application/zip"))
    plain = await indexed_note(maker)

    await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, EMR_JOBS, id=encrypted) == 0
    assert await count(maker, EMR_JOBS, id=plain) == 0


async def test_the_drain_waits_for_the_emr_parse_too(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`emr_parse` writes facts and moves the EMR projections but flips no
    `integration_state` of its own, so a drain gate watching only the note's graph
    producer would chain the wiki repair over a graph the parser was still writing — the
    exact damage (0045/0046) the chain exists to prevent."""
    await quiesce(maker)
    note = await emr_note(maker, media_types=("application/pdf",))
    await rebuild.rebuild_batch(maker, start=True)
    async with scoped_session(maker, OWNER) as s:
        # Integration itself is done; only the parse is still outstanding.
        await s.execute(text("UPDATE app.notes SET integration_state = 'integrated'"))
        await s.execute(text("UPDATE app.jobs SET status = 'done' WHERE kind <> 'emr_parse'"))

    assert await rebuild.rebuild_batch(maker) is not None
    assert (await run_row(maker)).status == "draining"

    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.jobs SET status = 'done' WHERE kind = 'emr_parse'"),
        )
    await rebuild.rebuild_batch(maker)
    assert (await run_row(maker)).status == "completed"
    assert await count(maker, EMR_JOBS, id=note) == 0


async def test_agent_episodes_survive_a_rebuild(maker: async_sessionmaker[AsyncSession]) -> None:
    """The privacy purge deletes an episode WHOLE (invariant #11). Nothing re-derives
    one, so doing that in a rebuild would be silent data loss, not a rebuild."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Episode Subject", status="confirmed")
    await seed_fact(maker, note, entity)
    episode = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_episodes (id, domain_scopes, body)"
                " VALUES (:id, ARRAY['general'], 'we talked about the seed note')"
            ),
            {"id": episode},
        )
        await s.execute(
            text(
                "INSERT INTO app.agent_episode_refs (id, episode_id, note_id)"
                " VALUES (gen_random_uuid(), :eid, :nid)"
            ),
            {"eid": episode, "nid": note},
        )

    await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, "SELECT count(*) FROM app.agent_episodes WHERE id = :id", id=episode)


async def test_rebuild_is_resumable_across_fires(maker: async_sessionmaker[AsyncSession]) -> None:
    """One transaction per note plus a durable cursor: each fire continues where the
    last stopped, and a fire past the end never re-purges what it already rebuilt."""
    await quiesce(maker)
    notes = sorted([await indexed_note(maker) for _ in range(3)])
    entity = await seed_entity(maker, "Resume Subject", status="confirmed")
    for note in notes:
        await seed_fact(maker, note, entity)

    first = await rebuild.rebuild_batch(maker, start=True, limit=1)
    assert first.processed_now == 1
    row = await run_row(maker)
    assert row.notes_done == 1
    assert str(row.cursor_note_id) == notes[0]
    assert row.status == "purging"
    # The fire enqueued its own continuation rather than stopping at the batch.
    assert await count(maker, QUEUED_JOBS, kind="graph_rebuild") == 1

    second = await rebuild.rebuild_batch(maker, limit=1)
    assert second.processed_now == 1
    assert (await run_row(maker)).notes_done == 2

    third = await rebuild.rebuild_batch(maker, limit=1)
    assert third.processed_now == 1
    row = await run_row(maker)
    assert row.notes_done == 3
    assert row.status == "draining"

    # Past the end: no work, no double-count, no re-purge.
    fourth = await rebuild.rebuild_batch(maker, limit=1)
    assert fourth.processed_now == 0
    assert (await run_row(maker)).notes_done == 3


async def test_a_second_start_continues_the_open_run(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Two "Run now" clicks must not open rival runs over one corpus — the second
    continues the first, from its cursor."""
    await quiesce(maker)
    for _ in range(2):
        await indexed_note(maker)

    await rebuild.rebuild_batch(maker, start=True, limit=1)
    assert await rebuild.start_run(maker) is None
    await rebuild.rebuild_batch(maker, start=True, limit=1)

    assert await count(maker, OPEN_RUNS) == 1
    assert (await run_row(maker)).notes_done == 2


async def test_rebuild_chains_into_a_wiki_rebuild_once_integration_drains(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`wiki_citations.fact_id` is ON DELETE SET NULL and `wiki_articles.entity_ref` has
    no FK, so a graph re-derive silently degrades published revisions to chunk-only
    claims and orphans articles. The sweep therefore is not done at the last purge."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Wiki Subject", status="confirmed")
    await seed_fact(maker, note, entity)

    await rebuild.rebuild_batch(maker, start=True)
    assert (await run_row(maker)).status == "draining"
    # Still integrating: no wiki rebuild yet, and the run stays open.
    await rebuild.rebuild_batch(maker)
    row = await run_row(maker)
    assert row.status == "draining"
    assert row.wiki_rebuild_job_id is None

    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.notes SET integration_state = 'integrated'"))
        await s.execute(text("UPDATE app.jobs SET status = 'done' WHERE kind = 'note_converse'"))

    progress = await rebuild.rebuild_batch(maker)

    assert progress.status == "completed"
    row = await run_row(maker)
    assert row.status == "completed"
    assert row.wiki_rebuild_job_id is not None
    (job,) = await fetch(
        maker,
        "SELECT payload->>'target' AS target FROM app.jobs"
        " WHERE kind = 'wiki_rebuild' AND status = 'queued'",
    )
    assert job.target == "all"

    # A later fire finds no open run and queues no second rebuild.
    assert (await rebuild.rebuild_batch(maker)).run_id is None
    assert await count(maker, QUEUED_JOBS, kind="wiki_rebuild") == 1


async def test_drain_fire_without_a_run_is_inert(maker: async_sessionmaker[AsyncSession]) -> None:
    """The recurring drain schedule fires every few minutes forever; with no run open it
    must do nothing at all and report zero work (so the worker reaps its run)."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Inert Subject", status="confirmed")
    fact = await seed_fact(maker, note, entity)

    progress = await rebuild.rebuild_batch(maker)

    assert progress.run_id is None
    assert progress.processed_now == 0
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=fact) == 1
    assert await count(maker, OPEN_RUNS) == 0


async def test_deleted_and_unindexed_notes_are_not_rebuilt(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The candidate corpus is live, indexed notes. A soft-deleted note's artifacts are
    the privacy purge's business, and an un-indexed note has no chunks to re-derive
    from — re-queuing either would be the sweep manufacturing work it cannot do."""
    await quiesce(maker)
    entity = await seed_entity(maker, "Skip Subject", status="confirmed")
    pending = await seed_note(maker)
    pending_fact = await seed_fact(maker, pending, entity)
    deleted = await indexed_note(maker)
    deleted_fact = await seed_fact(maker, deleted, entity)
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET deleted_at = now() WHERE id = :id"), {"id": deleted}
        )

    await rebuild.rebuild_batch(maker, start=True)

    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=pending_fact) == 1
    assert await count(maker, "SELECT count(*) FROM app.facts WHERE id = :id", id=deleted_fact) == 1
    assert (await run_row(maker)).notes_done == 0


async def test_drain_deadline_chains_the_wiki_rebuild_anyway(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """One note that can never integrate must not hold the wiki chain open forever —
    the damage that leaves (revisions degraded to chunk-only claims, articles pointing
    at replaced entity ids) is worse than a wiki rebuilt over a partly-drained graph."""
    await quiesce(maker)
    note = await indexed_note(maker)
    entity = await seed_entity(maker, "Stuck Subject", status="confirmed")
    await seed_fact(maker, note, entity)

    await rebuild.rebuild_batch(maker, start=True)
    assert (await run_row(maker)).status == "draining"

    # The note never integrates; backdate the run past the deadline.
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.graph_rebuild_runs SET started_at = now() -"
                " make_interval(hours => :h) WHERE status <> 'completed'"
            ),
            {"h": rebuild.DRAIN_DEADLINE_HOURS + 1},
        )

    progress = await rebuild.rebuild_batch(maker)

    assert progress.status == "completed"
    assert (await run_row(maker)).wiki_rebuild_job_id is not None
    assert await count(maker, QUEUED_JOBS, kind="wiki_rebuild") == 1
