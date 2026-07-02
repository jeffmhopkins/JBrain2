"""The two-tier cutover's open-card retirement against real Postgres
(docs/ENTITY_GRAPH_REFOCUS_PLAN.md §3 T1.3): the boot sweep deletes only OPEN
new_predicate cards — resolved/dismissed/deferred rows are human history and
survive — and is one-shot per database via a persisted app.settings marker, so
a card the owner REOPENS (back to status='open') survives every later boot.
A parked card left behind can still be reopened and resolved via
map_to_existing onto a DEMOTED seed row, because sync_predicates leaves
registry-absent seed rows in place (the prune was dropped: 0031 grants no
delete path, and the alias FK needs the row). Embeddings are faked.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from jbrain.analysis.predicates import (
    _RETIRE_SWEEP_MARKER_KEY,
    alias_canonicals,
    registry_seed_rows,
    retire_open_new_predicate_cards,
)
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.db.session import scoped_session
from jbrain.embed import PredicateEmbedder
from jbrain.queue import SYSTEM_CTX
from tests.conftest import docker_available
from tests.integration.test_extraction_pg import make_note, maker  # noqa: F401
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_MODEL = "test-embed-v1"


class _FakeEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]


async def _insert_card(
    maker,  # noqa: F811
    predicate: str,
    *,
    kind: str = "new_predicate",
    status: str = "open",
    domain: str = "general",
    suggestions: list[dict] | None = None,
) -> str:
    payload = {
        "predicate": predicate,
        "fact_kind": "relationship",
        "statement": f"x {predicate} y",
        "suggestions": suggestions or [],
    }
    async with scoped_session(maker, SYSTEM_CTX) as session:
        return (
            await session.execute(
                text(
                    "INSERT INTO app.review_items (id, kind, payload, status, domain_code)"
                    " VALUES (gen_random_uuid(), :k, cast(:p AS jsonb), :s, :d)"
                    " RETURNING id::text"
                ),
                {"k": kind, "p": json.dumps(payload), "s": status, "d": domain},
            )
        ).scalar_one()


async def _reset_sweep_marker(maker) -> None:  # noqa: F811
    """Un-set the one-shot marker so a test exercises a genuine first boot —
    the module's tests share one database. Upsert 'false', not DELETE:
    app.settings grants no DELETE (resetting a key is upserting a default)."""
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.settings (key, value) VALUES (:k, 'false'::jsonb)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value"
            ),
            {"k": _RETIRE_SWEEP_MARKER_KEY},
        )


async def _card_status(maker, card_id: str) -> str | None:  # noqa: F811
    async with scoped_session(maker, SYSTEM_CTX) as session:
        return (
            await session.execute(
                text("SELECT status FROM app.review_items WHERE id = :id"), {"id": card_id}
            )
        ).scalar_one_or_none()


async def _seed_fact(maker, predicate: str) -> str:  # noqa: F811
    """An active fact under `predicate` on a fresh entity; returns the fact id."""
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
        return (
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


async def test_sweep_retires_open_cards_only_and_is_idempotent(maker):  # noqa: F811
    await _reset_sweep_marker(maker)
    open_general = await _insert_card(maker, "zzqOpenGeneral")
    # A health-domain card proves the SYSTEM_CTX sweep crosses domain firewalls.
    open_health = await _insert_card(maker, "zzqOpenHealth", domain="health")
    resolved = await _insert_card(maker, "zzqResolved", status="resolved")
    dismissed = await _insert_card(maker, "zzqDismissed", status="dismissed")
    deferred = await _insert_card(maker, "zzqDeferred", status="deferred")
    other_kind = await _insert_card(maker, "zzqOtherKind", kind="ambiguous_mention")

    assert await retire_open_new_predicate_cards(maker) == 2

    assert await _card_status(maker, open_general) is None
    assert await _card_status(maker, open_health) is None
    assert await _card_status(maker, resolved) == "resolved"
    assert await _card_status(maker, dismissed) == "dismissed"
    assert await _card_status(maker, deferred) == "deferred"
    assert await _card_status(maker, other_kind) == "open"  # kind-scoped, not status-wide

    assert await retire_open_new_predicate_cards(maker) == 0  # marker set: later boots skip


async def test_reopened_card_survives_later_boots(maker):  # noqa: F811
    # THE reason the sweep is marker-gated, not merely status-filtered: the
    # worker calls it at every boot, and reopen_review returns a deferred card
    # to status='open' with resolution cleared to NULL — by row shape a legacy
    # backlog card. Without the marker the next boot would silently delete the
    # card the owner just un-parked.
    await _reset_sweep_marker(maker)
    parked = await _insert_card(maker, "zzqUnparked", status="deferred")
    backlog = await _insert_card(maker, "zzqBacklog")

    assert await retire_open_new_predicate_cards(maker) == 1  # first boot: backlog only
    assert await _card_status(maker, backlog) is None
    assert await _card_status(maker, parked) == "deferred"

    reopened = await SqlAnalysisRepo(maker).reopen_review(OWNER, parked)
    assert reopened is not None and reopened["status"] == "open"
    assert reopened["resolution"] is None  # indistinguishable from legacy backlog

    assert await retire_open_new_predicate_cards(maker) == 0  # next boot
    assert await _card_status(maker, parked) == "open"  # the un-parked card survived

    # Leave no open new_predicate card behind: the module's tests share one
    # database and reset the marker to exercise real sweeps.
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(text("DELETE FROM app.review_items WHERE id = :id"), {"id": parked})


async def test_parked_card_resolution_still_applies_after_sweep_and_sync(maker):  # noqa: F811
    # Owner decision #6: parked cards stay resolvable after the cutover. The
    # deferred card must survive the sweep, its DEMOTED (registry-absent)
    # suggestion target must survive a sync_predicates run — the no-prune pin:
    # the seed upsert may never drop or clobber a stale seed row, because
    # map_to_existing's record_predicate_alias INSERT carries an FK to it —
    # and the reopen + map_to_existing resolution must then fully apply.
    await _reset_sweep_marker(maker)  # a REAL first-boot sweep, not a marker skip
    async with scoped_session(maker, SYSTEM_CTX) as session:
        await session.execute(
            text(
                "INSERT INTO app.canonical_predicates"
                " (canonical_name, descriptor, value_shape, kind, origin)"
                " VALUES ('zzqDemotedTarget', 'd', 'ref', 'relationship', 'seed')"
                " ON CONFLICT (canonical_name) DO NOTHING"
            )
        )
    fact_id = await _seed_fact(maker, "zzqParkedDrift")
    card = await _insert_card(
        maker,
        "zzqParkedDrift",
        status="deferred",
        suggestions=[{"name": "zzqDemotedTarget", "score": 0.64}],
    )

    assert await retire_open_new_predicate_cards(maker) == 0  # parked -> not swept
    await PredicateEmbedder(maker, _FakeEmbed(), _MODEL).sync_predicates({})
    async with scoped_session(maker, SYSTEM_CTX) as session:
        names = set(
            (
                await session.execute(text("SELECT canonical_name FROM app.canonical_predicates"))
            ).scalars()
        )
    assert "zzqDemotedTarget" in names  # the demoted seed row survived the sync
    assert {r.canonical_name for r in registry_seed_rows()} <= names

    repo = SqlAnalysisRepo(maker)
    reopened = await repo.reopen_review(OWNER, card)
    assert reopened is not None and reopened["status"] == "open"
    resolved = await repo.resolve_review(
        OWNER, card, "map_to_existing", {"canonical_name": "zzqDemotedTarget"}
    )
    assert resolved is not None and resolved["status"] == "resolved"

    async with scoped_session(maker, SYSTEM_CTX) as session:
        fact_predicate = (
            await session.execute(
                text("SELECT predicate FROM app.facts WHERE id = :id"), {"id": fact_id}
            )
        ).scalar_one()
        aliases = await alias_canonicals(session, ["zzqParkedDrift"])
    assert fact_predicate == "zzqDemotedTarget"
    assert aliases == {"zzqparkeddrift": "zzqDemotedTarget"}
