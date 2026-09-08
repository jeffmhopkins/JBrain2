"""The seam the rebuild's spare set exists for, end to end against real Postgres.

The rest of the coverage is split either side of this: the integration tests in
test_graph_rebuild_pg.py prove spared ROWS survive the purge, and the unit tests in
test_supersession.py prove `decide()` returns a no-card refresh for hand-built
`FactView`s. Neither proves the join — that the REAL pipeline, re-loading the REAL rows
the purge spared, actually produces the shapes `decide()` needs. That join is the whole
design: sparing a row is worthless if re-integration then files a card anyway, and a
card kind whose fact the spare set misses is deleted with nothing to notice.

So this seeds a settled review card of EVERY kind that names a fact — `fact_a`/`fact_b`
(fact_conflict, attribute_collision, low_confidence), `fact_id`
(low_confidence_inference, domain_promotion, wiki_stale_claim) and `source_fact_id`
(inverse_proposal) — across every status that outlives the purge (`resolved`,
`dismissed`, `deferred`), all onto facts a real integration produced, all settled
through the real `resolve_review`. Each seeded payload uses the shape its PRODUCTION
filer writes; a seed that invents a shape tests the fixture, not the sweep. Then it runs the
real sweep and re-integrates through the real pipeline, and asserts the two things the
owner would notice: no NEW review card is filed, and the twins are refreshed in place
rather than landing as fresh rows beside the decision.

Fails without the complete spare set: `low_confidence_inference`'s reject retracts its
fact without pinning it (so a `fact_a`/`fact_b`-only spare set deletes it), and a
`deferred` card is not purged while a resolved/dismissed-only spare set leaves both its
facts to be deleted under it. And it fails without the retracted-twin branch's SECOND
discriminator: that same reject pins nothing, so a pinned-head-only guard lets the
rejected `headquarters` value be re-minted as a fresh active row beside the row the
owner rejected, with no card filed to say so.
"""

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

from jbrain.analysis import rebuild
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import (
    analyzer,
    extraction_payload,
    ingest,
    make_note,
)
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

SUBJECT = "Seamco"

# Note A's facts, one per review-card scenario. All `attribute` so `decide()`'s path is
# the single-head one the flood was reported against; distinct predicates so each
# scenario's identity key is its own and they cannot interfere. Exactly the per-note
# fact cap: one more and the extraction is truncated, which files a card of its own.
BASE_FACTS: dict[str, str] = {
    "industry": "logistics",  # collides with note B -> a REAL resolved card
    "sector": "freight",  # collides with note C -> a REAL card, then DEFERRED
    "headquarters": "Portland",  # low_confidence_inference, rejected
    "founded": "1999",  # domain_promotion, accepted
    "motto": "onward",  # wiki_stale_claim, dismissed
    "ticker": "SMC",  # inverse_proposal, dismissed
}
# The seventh scenario, on its own note for the same cap reason.
FOUNDER = {"founder": "Ada"}  # low_confidence, dismissed


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _fact(predicate: str, value: str) -> dict[str, Any]:
    return {
        "predicate": predicate,
        "qualifier": "",
        "kind": "attribute",
        "statement": f"{SUBJECT} {predicate} is {value}.",
        "value_json": {"text": value},
        "assertion": "asserted",
        "entity_ref": SUBJECT,
        "object_entity_ref": None,
        "temporal": None,
        "domain": "general",
        "confidence": 0.9,
    }


def _extraction(values: dict[str, str]) -> str:
    return json.dumps(
        extraction_payload(
            title=f"About {SUBJECT}",
            tags=["work"],
            mentions=[{"name": SUBJECT, "kind": "Organization", "surface_text": SUBJECT}],
            facts=[_fact(p, v) for p, v in values.items()],
            temporal_tokens=[],
        )
    )


async def _fetch(maker: async_sessionmaker[AsyncSession], sql: str, **p: Any) -> list[Any]:  # noqa: F811
    async with scoped_session(maker, OWNER) as s:
        return list((await s.execute(text(sql), p)).all())


async def _note(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path: Any,
    values: dict[str, str],
) -> str:
    """An ingested, integrated, sweep-eligible note asserting `values` about SUBJECT."""
    body = " ".join(f"{SUBJECT} {p} is {v}." for p, v in values.items())
    note = await make_note(maker, domain="general", body=body)
    await ingest(maker, note, tmp_path)
    await analyzer(maker, [_extraction(values)]).analyze_note({"note_id": note})
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "UPDATE app.notes SET ingest_state = 'indexed',"
                " integration_state = 'integrated' WHERE id = :id"
            ),
            {"id": note},
        )
    return note


async def _fact_id(maker: async_sessionmaker[AsyncSession], note: str, predicate: str) -> str:  # noqa: F811
    (row,) = await _fetch(
        maker,
        "SELECT id::text AS id FROM app.facts WHERE note_id = :n AND predicate = :p",
        n=note,
        p=predicate,
    )
    return str(row.id)


async def _open_card(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    kind: str,
    payload: dict[str, Any],
) -> str:
    """An OPEN card of `kind`; the test settles it through the real `resolve_review`, so
    the effects a reopen would replay are the production ones, not a fixture's guess."""
    iid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.review_items (id, kind, payload, status, domain_code)"
                " VALUES (:id, :kind, cast(:payload AS jsonb), 'open', 'general')"
            ),
            {"id": iid, "kind": kind, "payload": json.dumps(payload)},
        )
    return iid


async def _collision_card(maker: async_sessionmaker[AsyncSession], predicate: str) -> str:  # noqa: F811
    """The open attribute_collision the pipeline itself filed for `predicate`."""
    (row,) = await _fetch(
        maker,
        "SELECT id::text AS id FROM app.review_items WHERE kind = 'attribute_collision'"
        " AND status = 'open' AND payload->>'predicate' = :p",
        p=predicate,
    )
    return str(row.id)


async def test_a_settled_card_of_every_fact_naming_kind_survives_a_rebuild_unchanged(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path: Any,
) -> None:
    repo = SqlAnalysisRepo(maker)

    # --- four real integrations: the base note, the two that collide with it (so the
    # pinned/retracted and the deferred pairs are the pipeline's own rows, not seeds),
    # and the overflow note the fact cap forced.
    note_a = await _note(maker, tmp_path, BASE_FACTS)
    note_b = await _note(maker, tmp_path, {"industry": "shipping"})
    note_c = await _note(maker, tmp_path, {"sector": "haulage"})
    note_d = await _note(maker, tmp_path, FOUNDER)

    ids = {p: await _fact_id(maker, note_a, p) for p in BASE_FACTS}
    ids["founder"] = await _fact_id(maker, note_d, "founder")
    industry_b = await _fact_id(maker, note_b, "industry")
    sector_c = await _fact_id(maker, note_c, "sector")

    # resolved, fact_a/fact_b: the owner picks A's value. Pins it, retracts B's — and
    # B's row is now reachable by no supersession walk at all.
    await repo.resolve_review(OWNER, await _collision_card(maker, "industry"), "accept_a", {})
    # deferred, fact_a/fact_b: parked, NEITHER side pinned. The purge keeps the card
    # (it only retires open ones), so it must keep both facts the card will cite when
    # the owner un-parks it back into the open queue.
    deferred_card = await _collision_card(maker, "sector")
    await repo.resolve_review(OWNER, deferred_card, "defer", {})

    # resolved, fact_id: reject retracts the held row and does NOT pin it, so neither
    # the pin walk nor a fact_a/fact_b lookup can see it.
    lci_card = await _open_card(
        maker, "low_confidence_inference", {"fact_id": ids["headquarters"], "note_id": note_a}
    )
    await repo.resolve_review(OWNER, lci_card, "reject", {})
    # resolved, fact_id: accept pins, so this one already survives on the pin walk —
    # spared through the shared key anyway, so it stops depending on repo.py's verbs.
    promo_card = await _open_card(
        maker, "domain_promotion", {"fact_id": ids["founded"], "proposed_domain": "general"}
    )
    await repo.resolve_review(OWNER, promo_card, "accept", {})
    # dismissed, one per remaining fact-naming kind and key.
    stale_card = await _open_card(maker, "wiki_stale_claim", {"fact_id": ids["motto"]})
    # `source_fact_id`, not fact_a/fact_b: inverse_proposal is filed by its own writer
    # (pipeline.py `_write_inverse`), naming the primary fact the refused reciprocal
    # would have mirrored.
    inverse_card = await _open_card(
        maker, "inverse_proposal", {"source_fact_id": ids["ticker"], "note_id": note_a}
    )
    lowconf_card = await _open_card(maker, "low_confidence", {"fact_a": ids["founder"]})
    for card in (stale_card, inverse_card, lowconf_card):
        await repo.resolve_review(OWNER, card, "dismiss", {})

    settled = {
        deferred_card: "deferred",
        lci_card: "resolved",
        promo_card: "resolved",
        stale_card: "dismissed",
        inverse_card: "dismissed",
        lowconf_card: "dismissed",
    }
    named = {*ids.values(), industry_b, sector_c}
    cards_before = {
        r.id for r in await _fetch(maker, "SELECT id::text AS id FROM app.review_items")
    }

    # --- the sweep, then re-integration through the real pipeline.
    await rebuild.rebuild_batch(maker, start=True)

    survivors = {
        str(r.id)
        for r in await _fetch(
            maker,
            "SELECT id::text AS id FROM app.facts WHERE id = ANY(cast(:ids AS uuid[]))",
            ids=list(named),
        )
    }
    assert survivors == named, "a fact a surviving review card names was purged"

    await analyzer(maker, [_extraction(BASE_FACTS)]).analyze_note({"note_id": note_a})
    await analyzer(maker, [_extraction({"industry": "shipping"})]).analyze_note({"note_id": note_b})
    await analyzer(maker, [_extraction({"sector": "haulage"})]).analyze_note({"note_id": note_c})
    await analyzer(maker, [_extraction(FOUNDER)]).analyze_note({"note_id": note_d})

    # --- nothing was re-litigated.
    cards_after = await _fetch(maker, "SELECT id::text AS id, status FROM app.review_items")
    assert {r.id for r in cards_after} - cards_before == set(), "the rebuild re-filed a card"
    assert {r.id: r.status for r in cards_after if r.id in settled} == settled

    # Every twin was refreshed in place: the identity key still holds exactly the rows
    # the decision left it holding, with their original ids and statuses. `headquarters`
    # is the case with no pinned head at all — the reject retracted the row and pinned
    # nothing — so it is the one the resolution's recorded `retracted` effect has to
    # carry: the rejected value must stay rejected and gain no fresh active twin beside
    # itself.
    for predicate, expected in (
        ("industry", {(ids["industry"], "active"), (industry_b, "retracted")}),
        ("sector", {(ids["sector"], "pending_review"), (sector_c, "pending_review")}),
        ("headquarters", {(ids["headquarters"], "retracted")}),
        ("founded", {(ids["founded"], "active")}),
        ("motto", {(ids["motto"], "active")}),
        ("ticker", {(ids["ticker"], "active")}),
        ("founder", {(ids["founder"], "active")}),
    ):
        rows = await _fetch(
            maker,
            "SELECT id::text AS id, status FROM app.facts WHERE predicate = :p",
            p=predicate,
        )
        assert {(str(r.id), r.status) for r in rows} == expected, predicate
