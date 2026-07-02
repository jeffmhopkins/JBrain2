"""The graph-view read paths against real Postgres: GET /api/entities/{id}/
neighbors (the ego subgraph), GET /api/graph (the whole-graph default,
disconnected entities included, centered on "Me"), and the agent's
`SqlAnalysisRepo.neighborhood` n-hop traversal (refs + co-mentions) — depth,
directed edges, hub damping, merged tombstones and deleted notes excluded, and
the RLS firewall (CLAUDE.md rule 3 — every new read path needs an isolation
test, including the mid-traversal vanish of a firewalled branch).

The module database is shared, so the seed tags every name/kind with a unique
suffix and assertions filter to this seed's ids before comparing.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.main import create_app
from jbrain.models.analysis import Entity, EntityMention, Fact
from jbrain.models.notes import Chunk, Note
from jbrain.notes.repo import SqlNotesRepo
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
FINANCE_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("finance",))


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def rel(
    src: uuid.UUID,
    dst: uuid.UUID,
    note_id: uuid.UUID,
    predicate: str,
    domain: str,
    *,
    reported_at: datetime | None = None,
    assertion: str = "asserted",
    status: str = "active",
) -> Fact:
    """A directed relationship edge src --predicate--> dst."""
    return Fact(
        entity_id=src,
        object_entity_id=dst,
        predicate=predicate,
        qualifier="",
        kind="relationship",
        statement=f"{predicate} edge",
        assertion=assertion,
        status=status,
        valid_from=None,
        reported_at=reported_at or datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        note_id=note_id,
        extractor="test",
        prompt_version="test",
        domain_code=domain,
    )


@pytest.fixture
async def seeded(maker: async_sessionmaker[AsyncSession], tmp_path: Any) -> dict[str, str]:
    """An ego network around "Me", ref edges plus a co-mention web:

    me --spouse--> wife           (general, 1 hop)
    me --worksFor--> employer     (general, 1 hop)
    me --seenAt--> clinic         (HEALTH, 1 hop)
    boss --manages--> me          (general, 1 hop — reachable ONLY through the
                                   inbound arm, the positive in_sql fixture)
    me --memberOf--> oldgym       (general, status=RETRACTED — never surfaced)
    me --enemyOf--> foe           (general, assertion=NEGATED — never surfaced)
    wife --sibling--> distant     (general, 2 hops)
    wife --children--> kid        (general, 2 hops; kid --parent--> wife is its
                                   NEWER derived reciprocal shadow — dedup bait)
    clinic --prescribes--> med    (HEALTH, 2 hops)
    nurse --referredTo--> specialist (HEALTH, 2 hops behind a co-mention)
    boss --knows--> nurse         (a GENERAL fact pointing at a HEALTH entity —
                                   production ratchets fact domains from the
                                   note, so the mismatch is real: under a
                                   general-only session the fact row is
                                   visible and only the entities INNER JOIN
                                   drops the edge — the REF-arm LEFT-JOIN trap,
                                   twin of note_g's co-mention trap below)
    + co-mention chain: note_c1 {me, colleague, wife, gone}, note_c2
      {colleague, mentor}, note_c3 {mentor, protege} — hops 1/2/3 by shared note
    + note_h (HEALTH) {clinic, nurse} — the health branch's co-mention arm
    + note_g (GENERAL) {me, nurse} — a general note mentioning a HEALTH entity:
      the mention rows are note-domain (general, the production stamp) while
      the nurse entity keeps health, so a general-only session sees the mention
      but the entities INNER JOIN must drop the edge (the LEFT-JOIN leak trap)
    + note_hub: me + 14 guests, 15 distinct entities — over hub_cap, never
      expanded through
    + note_del (soft-deleted) {me, ghostpal} — live-notes-only exclusion
    + an island entity with no edges (only the full graph surfaces it)
    + a merged tombstone me points at (and note_c1 mentions), never surfaced.
    """
    tag = uuid.uuid4().hex[:8]
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"nb-{tag}", domain="general", destination=None, body="seed"
    )
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note.id})
    note_id = uuid.UUID(note.id)
    async with scoped_session(maker, OWNER) as session:

        def ent(name: str, kind: str, domain: str, status: str = "confirmed") -> Entity:
            return Entity(
                kind=f"{kind}-{tag}",
                canonical_name=f"{name} {tag}",
                status=status,
                domain_code=domain,
            )

        me = ent("Me", "Person", "general")
        wife = ent("Wife", "Person", "general")
        employer = ent("Acme", "Organization", "general")
        clinic = ent("Clinic", "Organization", "health")
        distant = ent("Sibling", "Person", "general")
        med = ent("Med", "Drug", "health")
        island = ent("Island", "Place", "general")
        kid = ent("Kid", "Person", "general")
        colleague = ent("Colleague", "Person", "general")
        mentor = ent("Mentor", "Person", "general")
        protege = ent("Protege", "Person", "general")
        nurse = ent("Nurse", "Person", "health")
        specialist = ent("Specialist", "Person", "health")
        ghostpal = ent("Ghostpal", "Person", "general")
        boss = ent("Boss", "Person", "general")
        oldgym = ent("Oldgym", "Organization", "general")
        foe = ent("Foe", "Person", "general")
        guests = [ent(f"Guest{i}", "Person", "general") for i in range(14)]
        session.add_all(
            [me, wife, employer, clinic, distant, med, island, kid]
            + [colleague, mentor, protege, nurse, specialist, ghostpal]
            + [boss, oldgym, foe]
            + guests
        )
        await session.flush()
        gone = ent("Ghost", "Person", "general", status="merged")
        gone.merged_into_id = wife.id
        session.add(gone)
        await session.flush()
        kid_primary = rel(wife.id, kid.id, note_id, "children", "general")
        session.add(kid_primary)
        await session.flush()
        # The pipeline-materialized inverse of the wife's own children fact,
        # deliberately NEWER so it would out-rank the primary and flip the
        # kid's connecting path if the inbound arm failed to drop it.
        kid_shadow = rel(
            kid.id,
            wife.id,
            note_id,
            "parent",
            "general",
            reported_at=datetime(2026, 6, 5, 12, 0, tzinfo=UTC),
        )
        kid_shadow.derived_from_fact_id = kid_primary.id
        session.add_all(
            [
                kid_shadow,
                rel(me.id, wife.id, note_id, "spouse", "general"),
                rel(me.id, employer.id, note_id, "worksFor", "general"),
                rel(me.id, clinic.id, note_id, "seenAt", "health"),
                rel(wife.id, distant.id, note_id, "sibling", "general"),
                rel(clinic.id, med.id, note_id, "prescribes", "health"),
                rel(me.id, gone.id, note_id, "knows", "general"),
                rel(nurse.id, specialist.id, note_id, "referredTo", "health"),
                rel(boss.id, me.id, note_id, "manages", "general"),
                rel(boss.id, nurse.id, note_id, "knows", "general"),
                rel(me.id, oldgym.id, note_id, "memberOf", "general", status="retracted"),
                rel(me.id, foe.id, note_id, "enemyOf", "general", assertion="negated"),
            ]
        )

        def day(d: int) -> datetime:
            return datetime(2026, 6, d, 12, 0, tzinfo=UTC)

        notes = {
            "note_c1": Note(client_id=f"nb-{tag}-c1", domain_code="general", body="c1"),
            "note_c2": Note(client_id=f"nb-{tag}-c2", domain_code="general", body="c2"),
            "note_c3": Note(client_id=f"nb-{tag}-c3", domain_code="general", body="c3"),
            "note_h": Note(client_id=f"nb-{tag}-h", domain_code="health", body="h"),
            "note_g": Note(client_id=f"nb-{tag}-g", domain_code="general", body="g"),
            "note_del": Note(client_id=f"nb-{tag}-del", domain_code="general", body="del"),
            "note_hub": Note(client_id=f"nb-{tag}-hub", domain_code="general", body="hub"),
        }
        for key, d in (
            ("note_c1", 10),
            ("note_c2", 11),
            ("note_c3", 12),
            ("note_h", 15),
            ("note_del", 16),
            ("note_g", 18),
            ("note_hub", 20),
        ):
            notes[key].created_at = day(d)
        notes["note_del"].deleted_at = day(17)
        session.add_all(notes.values())
        await session.flush()
        chunks = {
            key: Chunk(
                note_id=n.id, domain_code=n.domain_code, granularity="paragraph", seq=0, text=n.body
            )
            for key, n in notes.items()
        }
        session.add_all(chunks.values())
        await session.flush()

        def mention(entity: Entity, key: str) -> EntityMention:
            return EntityMention(
                entity_id=entity.id,
                chunk_id=chunks[key].id,
                note_id=notes[key].id,
                surface_text=entity.canonical_name,
                char_start=0,
                char_end=1,
                link_method="human",
                domain_code=notes[key].domain_code,
            )

        session.add_all(
            [mention(e, "note_c1") for e in (me, colleague, wife, gone)]
            + [mention(e, "note_c2") for e in (colleague, mentor)]
            + [mention(e, "note_c3") for e in (mentor, protege)]
            + [mention(e, "note_h") for e in (clinic, nurse)]
            + [mention(e, "note_g") for e in (me, nurse)]
            + [mention(e, "note_del") for e in (me, ghostpal)]
            + [mention(e, "note_hub") for e in (me, *guests)]
        )
    return {
        "tag": tag,
        "me": str(me.id),
        "wife": str(wife.id),
        "employer": str(employer.id),
        "clinic": str(clinic.id),
        "distant": str(distant.id),
        "med": str(med.id),
        "island": str(island.id),
        "gone": str(gone.id),
        "kid": str(kid.id),
        "colleague": str(colleague.id),
        "mentor": str(mentor.id),
        "protege": str(protege.id),
        "nurse": str(nurse.id),
        "specialist": str(specialist.id),
        "ghostpal": str(ghostpal.id),
        "boss": str(boss.id),
        "oldgym": str(oldgym.id),
        "foe": str(foe.id),
        **{key: str(n.id) for key, n in notes.items()},
    }


def node_ids(graph: dict[str, Any]) -> set[str]:
    return {n["id"] for n in graph["nodes"]}


def edge_set(graph: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(e["source"], e["target"], e["predicate"]) for e in graph["edges"]}


async def test_depth_one_is_focal_plus_direct_neighbours(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    graph = await SqlAnalysisRepo(maker).ego_graph(OWNER, seeded["me"], depth=1)
    assert graph is not None
    assert graph["root"] == seeded["me"] and graph["depth"] == 1
    # Both directions: the boss's inbound manages edge counts as one hop. The
    # exact set also pins the retracted/negated exclusion (oldgym, foe).
    assert node_ids(graph) == {
        seeded["me"],
        seeded["wife"],
        seeded["employer"],
        seeded["clinic"],
        seeded["boss"],
    }
    # Directed edges, deduped, the merged tombstone excluded.
    assert (seeded["me"], seeded["wife"], "spouse") in edge_set(graph)
    assert (seeded["me"], seeded["clinic"], "seenAt") in edge_set(graph)
    assert (seeded["boss"], seeded["me"], "manages") in edge_set(graph)
    assert seeded["gone"] not in node_ids(graph)
    assert seeded["med"] not in node_ids(graph)  # 2 hops away, not at depth 1


async def test_depth_two_reaches_second_hop(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    graph = await SqlAnalysisRepo(maker).ego_graph(OWNER, seeded["me"], depth=2)
    assert graph is not None
    assert {seeded["distant"], seeded["med"]} <= node_ids(graph)
    assert (seeded["wife"], seeded["distant"], "sibling") in edge_set(graph)
    assert (seeded["clinic"], seeded["med"], "prescribes") in edge_set(graph)


async def test_neighbors_are_rls_scoped(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    graph = await repo.ego_graph(GENERAL_ONLY, seeded["me"], depth=2)
    assert graph is not None
    ids = node_ids(graph)
    # The health firewall holds: the clinic, the edge to it, and the drug
    # reachable only through it all vanish for a general-only session.
    assert seeded["clinic"] not in ids
    assert seeded["med"] not in ids
    assert not any(e["target"] == seeded["clinic"] for e in graph["edges"])
    # General neighbours still resolve.
    assert {seeded["wife"], seeded["employer"], seeded["distant"]} <= ids
    # An unscoped principal can't even see the focal entity.
    assert await repo.ego_graph(UNSCOPED, seeded["me"], depth=1) is None


async def test_unknown_or_malformed_id(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    assert await repo.ego_graph(OWNER, "not-a-uuid", depth=1) is None
    assert await repo.ego_graph(OWNER, str(uuid.uuid4()), depth=1) is None


async def test_neighbors_api_round_trip(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
    seeded: dict[str, str],
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        login = client.post("/api/auth/session", json={"owner_key": key, "device_label": "it"})
        assert login.status_code == 204
        graph = client.get(f"/api/entities/{seeded['me']}/neighbors", params={"depth": 2}).json()
        assert graph["root"] == seeded["me"] and graph["depth"] == 2
        assert {n["id"] for n in graph["nodes"]} >= {seeded["wife"], seeded["med"]}
        # Frozen wire shape for the frontend.
        assert set(graph) == {"root", "depth", "nodes", "edges"}
        assert set(graph["nodes"][0]) == {"id", "kind", "canonical_name", "status", "domain"}
        assert set(graph["edges"][0]) == {"source", "target", "predicate"}
        # depth is clamped to [1, 2] by the query validator.
        too_deep = client.get(f"/api/entities/{seeded['me']}/neighbors", params={"depth": 9})
        assert too_deep.status_code == 422  # depth clamped to [1, 2] by the validator
        assert client.get(f"/api/entities/{uuid.uuid4()}/neighbors").status_code == 404


async def _seed_me_subject(session: AsyncSession, tag: str) -> str:
    """A subject-backed entity named exactly "Me" — the graph's natural center,
    which `full_graph` resolves as its root."""
    sub = uuid.uuid4()
    me = uuid.uuid4()
    await session.execute(
        text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:id, 'Me', 'person')"),
        {"id": str(sub)},
    )
    await session.execute(
        text(
            "INSERT INTO app.entities"
            " (id, kind, canonical_name, status, subject_id, domain_code)"
            " VALUES (:id, :kind, 'Me', 'confirmed', :sub, 'general')"
        ),
        {"id": str(me), "kind": f"Person-{tag}", "sub": str(sub)},
    )
    return str(me)


async def test_full_graph_includes_disconnected_entities_and_all_edges(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    graph = await SqlAnalysisRepo(maker).full_graph(OWNER)
    ids = node_ids(graph)
    # Every seeded entity is present — the island included, which no ego view
    # would ever reach since it has no edges.
    assert {
        seeded["me"],
        seeded["wife"],
        seeded["employer"],
        seeded["clinic"],
        seeded["distant"],
        seeded["med"],
        seeded["island"],
    } <= ids
    assert seeded["gone"] not in ids  # merged tombstones never surface
    # Edges span the whole graph, not just one entity's neighbourhood.
    es = edge_set(graph)
    assert (seeded["me"], seeded["wife"], "spouse") in es
    assert (seeded["clinic"], seeded["med"], "prescribes") in es


async def test_full_graph_centers_on_me(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    async with scoped_session(maker, OWNER) as session:
        me = await _seed_me_subject(session, seeded["tag"])
    graph = await SqlAnalysisRepo(maker).full_graph(OWNER)
    by_id = {n["id"]: n for n in graph["nodes"]}
    # Root resolves to the subject-backed "Me", the view's natural center.
    assert graph["root"] == me
    assert by_id[graph["root"]]["canonical_name"].lower() == "me"
    assert graph["depth"] == 0  # the whole-graph sentinel


async def test_full_graph_is_rls_scoped(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    graph = await SqlAnalysisRepo(maker).full_graph(GENERAL_ONLY)
    ids = node_ids(graph)
    # The health firewall holds in the everything-graph too: the clinic, the
    # drug behind it, and every edge touching the clinic all vanish.
    assert seeded["clinic"] not in ids and seeded["med"] not in ids
    assert not any(
        e["source"] == seeded["clinic"] or e["target"] == seeded["clinic"] for e in graph["edges"]
    )
    # General entities — connected and disconnected alike — still resolve.
    assert {seeded["me"], seeded["wife"], seeded["island"]} <= ids


# --- SqlAnalysisRepo.neighborhood (Wave 3 T3.2) -----------------------------


def entity_ids(result: dict[str, Any]) -> set[str]:
    return {e["id"] for e in result["entities"]}


def hops_by_id(result: dict[str, Any]) -> dict[str, int]:
    return {e["id"]: e["hop"] for e in result["entities"]}


def note_ids(result: dict[str, Any]) -> list[str]:
    return [n["note_id"] for n in result["notes"]]


async def test_neighborhood_depth_semantics(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    d1 = await repo.neighborhood(OWNER, seeded["me"], depth=1)
    assert d1 is not None
    assert d1["anchor"] == seeded["me"] and d1["depth"] == 1
    # boss arrives through the inbound ref arm, nurse through the general
    # co-mention note; the exact set also pins the status='active' and
    # assertion='asserted' filters (oldgym is retracted, foe is negated).
    hop1 = {seeded[k] for k in ("wife", "employer", "clinic", "colleague", "boss", "nurse")}
    assert entity_ids(d1) == {seeded["me"]} | hop1

    d2 = await repo.neighborhood(OWNER, seeded["me"], depth=2)
    assert d2 is not None
    hop2 = {seeded[k] for k in ("distant", "kid", "med", "mentor", "specialist")}
    assert entity_ids(d2) == entity_ids(d1) | hop2

    d3 = await repo.neighborhood(OWNER, seeded["me"], depth=3)
    assert d3 is not None
    hop3 = {seeded["protege"]}
    assert entity_ids(d3) == entity_ids(d2) | hop3
    hops = hops_by_id(d3)
    assert hops[seeded["me"]] == 0
    assert all(hops[i] == 1 for i in hop1)
    assert all(hops[i] == 2 for i in hop2)
    assert all(hops[i] == 3 for i in hop3)
    # Depth clamps to 1..3, ego_graph-style.
    shallow = await repo.neighborhood(OWNER, seeded["me"], depth=0)
    assert shallow is not None and shallow["depth"] == 1
    deep = await repo.neighborhood(OWNER, seeded["me"], depth=9)
    assert deep is not None and deep["depth"] == 3 and entity_ids(deep) == entity_ids(d3)


async def test_neighborhood_paths_mix_edge_kinds_and_dedup_derived_shadow(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    d2 = await SqlAnalysisRepo(maker).neighborhood(OWNER, seeded["me"], depth=2)
    assert d2 is not None
    tag = seeded["tag"]
    paths = {e["id"]: e["path"] for e in d2["entities"]}
    assert paths[seeded["wife"]] == f"Me {tag} -spouse-> Wife {tag}"
    # An inbound ref renders arrow-reversed: the frontier entity stays on the
    # left, the fact's subject on the right.
    assert paths[seeded["boss"]] == f"Me {tag} <-manages- Boss {tag}"
    assert (
        paths[seeded["colleague"]]
        == f"Me {tag} -co-mention(note {seeded['note_c1']})-> Colleague {tag}"
    )
    assert paths[seeded["mentor"]] == (
        f"Me {tag} -co-mention(note {seeded['note_c1']})-> Colleague {tag}"
        f" -co-mention(note {seeded['note_c2']})-> Mentor {tag}"
    )
    assert paths[seeded["nurse"]] == f"Me {tag} -co-mention(note {seeded['note_g']})-> Nurse {tag}"
    # Edge kinds mix along one path: a co-mention hop then a ref hop.
    assert paths[seeded["specialist"]] == (
        f"Me {tag} -co-mention(note {seeded['note_g']})-> Nurse {tag}"
        f" -referredTo-> Specialist {tag}"
    )
    # The kid's NEWER derived reciprocal (kid --parent--> wife) was dropped on
    # the inbound arm, so the connecting edge is the wife's own children fact.
    assert paths[seeded["kid"]] == f"Me {tag} -spouse-> Wife {tag} -children-> Kid {tag}"


async def test_neighborhood_kinds_narrows_to_one_edge_arm(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    """The agent tool's kinds argument: each arm walks alone, and notes still
    collect from mentions of whatever entity set that walk reached."""
    repo = SqlAnalysisRepo(maker)
    refs = await repo.neighborhood(OWNER, seeded["me"], depth=2, kinds="relationships")
    assert refs is not None
    # Only fact edges: the co-mention arrivals (colleague, mentor) vanish and
    # the ref web survives whole. Nurse still arrives — at hop 2 now, through
    # boss's cross-domain knows fact instead of the co-mention note.
    assert entity_ids(refs) == {
        seeded[k]
        for k in ("me", "wife", "employer", "clinic", "boss", "distant", "kid", "med", "nurse")
    }
    # note_c1 still surfaces — wife (a ref arrival) is mentioned in it.
    assert seeded["note_c1"] in note_ids(refs)
    co = await repo.neighborhood(OWNER, seeded["me"], depth=2, kinds="co-mentions")
    assert co is not None
    # Only shared notes: wife now arrives via note_c1 and clinic via note_h
    # behind the nurse; the ref-only branch (employer, boss, kid, …) vanishes.
    assert entity_ids(co) == {
        seeded[k] for k in ("me", "wife", "colleague", "nurse", "mentor", "clinic")
    }
    paths = {e["id"]: e["path"] for e in co["entities"]}
    tag = seeded["tag"]
    assert paths[seeded["wife"]] == f"Me {tag} -co-mention(note {seeded['note_c1']})-> Wife {tag}"


async def test_neighborhood_hub_note_damped_but_still_listed(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    d3 = await repo.neighborhood(OWNER, seeded["me"], depth=3)
    assert d3 is not None
    # 15 distinct entities > hub_cap 8: never expanded through...
    assert not any(e["name"].startswith("Guest") for e in d3["entities"])
    # ...but the hub note itself still surfaces via the anchor's mention.
    hub = next(n for n in d3["notes"] if n["note_id"] == seeded["note_hub"])
    assert hub["hop"] == 0 and hub["connects"] == [f"Me {seeded['tag']}"]
    # hub_cap is a tunable argument: raised past the fan-out, the guests flow.
    wide = await repo.neighborhood(OWNER, seeded["me"], depth=1, hub_cap=20)
    assert wide is not None
    assert sum(1 for e in wide["entities"] if e["name"].startswith("Guest")) == 14


async def test_neighborhood_notes_min_hop_then_recency_and_cap(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    d2 = await repo.neighborhood(OWNER, seeded["me"], depth=2)
    assert d2 is not None
    # min(hop) stamps (hub 0 / g 0 / c1 0 / h 1 / c2 1 / c3 2), newest-first
    # within a hop; the deleted note never appears (see the exclusion test).
    expected = [
        seeded[k] for k in ("note_hub", "note_g", "note_c1", "note_h", "note_c2", "note_c3")
    ]
    assert note_ids(d2) == expected
    by_note = {n["note_id"]: n for n in d2["notes"]}
    tag = seeded["tag"]
    # Connecting names in hop-then-name order; the merged Ghost mention and
    # entities outside the traversed set contribute nothing.
    assert by_note[seeded["note_c1"]]["hop"] == 0
    assert by_note[seeded["note_c1"]]["connects"] == [
        f"Me {tag}",
        f"Colleague {tag}",
        f"Wife {tag}",
    ]
    assert by_note[seeded["note_h"]]["hop"] == 1  # min over clinic(1), nurse(1)
    assert by_note[seeded["note_h"]]["connects"] == [f"Clinic {tag}", f"Nurse {tag}"]
    assert by_note[seeded["note_g"]]["connects"] == [f"Me {tag}", f"Nurse {tag}"]
    # The note's domain rides along for the agent tool's source chips.
    assert by_note[seeded["note_h"]]["domain"] == "health"
    assert by_note[seeded["note_c1"]]["domain"] == "general"
    capped = await repo.neighborhood(OWNER, seeded["me"], depth=2, note_cap=2)
    assert capped is not None and note_ids(capped) == expected[:2]


async def test_neighborhood_excludes_merged_tombstones_and_deleted_notes(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    d3 = await repo.neighborhood(OWNER, seeded["me"], depth=3)
    assert d3 is not None
    # The tombstone is both ref-linked (me --knows--> gone) and co-mentioned
    # (note_c1): neither edge kind may surface it.
    assert seeded["gone"] not in entity_ids(d3)
    # A retracted fact (me --memberOf--> oldgym) and a negated one
    # (me --enemyOf--> foe) are dead edges: their targets never surface.
    assert seeded["oldgym"] not in entity_ids(d3)
    assert seeded["foe"] not in entity_ids(d3)
    # Ghostpal's only connection is through the soft-deleted note.
    assert seeded["ghostpal"] not in entity_ids(d3)
    assert seeded["note_del"] not in note_ids(d3)
    # A merged anchor is invisible, like ego_graph.
    assert await repo.neighborhood(OWNER, seeded["gone"]) is None


async def test_neighborhood_unknown_or_malformed_anchor(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    repo = SqlAnalysisRepo(maker)
    assert await repo.neighborhood(OWNER, "not-a-uuid") is None
    assert await repo.neighborhood(OWNER, str(uuid.uuid4())) is None


async def test_neighborhood_is_rls_scoped(
    maker: async_sessionmaker[AsyncSession], seeded: dict[str, str]
) -> None:
    """CLAUDE.md rule 3: the health branch vanishes transitively for a
    general-only session through BOTH edge kinds — the hop-1 ref (clinic), the
    hop-2 ref behind it (med), the hop-1 co-mention (nurse), and the ref
    behind that co-mention (specialist)."""
    repo = SqlAnalysisRepo(maker)
    d3 = await repo.neighborhood(GENERAL_ONLY, seeded["me"], depth=3)
    assert d3 is not None
    health = {seeded[k] for k in ("clinic", "med", "nurse", "specialist")}
    assert not entity_ids(d3) & health
    # The general branch still resolves in full, hops 1-3 (boss inbound too).
    general = {
        seeded[k]
        for k in ("wife", "employer", "boss", "colleague", "distant", "kid", "mentor", "protege")
    }
    assert entity_ids(d3) == {seeded["me"]} | general
    # note_g is the production firewall state (pipeline stamps mentions with
    # the NOTE's domain): its general mention rows — nurse's included — are
    # visible to this session while the nurse ENTITY is not, so the co-mention
    # queries' INNER JOIN to entities is what drops the edge. A LEFT JOIN
    # would leak a placeholder endpoint here. The REF arm is trapped the same
    # way: boss's general-domain knows fact points at the health nurse, so the
    # fact row is visible at hop 2 and only the ref queries' entities INNER
    # JOIN keeps the nurse out of the exact set above.
    assert all(e["id"] and e["name"] for e in d3["entities"])
    by_note = {n["note_id"]: n for n in d3["notes"]}
    assert by_note[seeded["note_g"]]["connects"] == [f"Me {seeded['tag']}"]
    # The health note is gone from the notes result, and no health NAME leaks
    # into any rendered path or connects line (the bare-uuid/LEFT-JOIN trap).
    assert seeded["note_h"] not in note_ids(d3)
    tag = seeded["tag"]
    blob = json.dumps(d3)
    for name in ("Clinic", "Med", "Nurse", "Specialist"):
        assert f"{name} {tag}" not in blob
    # An unscoped session sees nothing — not even the anchor — and a foreign
    # single-domain session is equally blind to this general-domain graph.
    assert await repo.neighborhood(UNSCOPED, seeded["me"], depth=3) is None
    assert await repo.neighborhood(FINANCE_ONLY, seeded["me"], depth=3) is None


async def test_full_graph_api_round_trip(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
    seeded: dict[str, str],
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        login = client.post("/api/auth/session", json={"owner_key": key, "device_label": "it"})
        assert login.status_code == 204
        graph = client.get("/api/graph").json()
        ids = {n["id"] for n in graph["nodes"]}
        assert {seeded["island"], seeded["wife"]} <= ids
        # Frozen wire shape — identical to the ego view so the UI reuses it.
        assert set(graph) == {"root", "depth", "nodes", "edges"}
        assert set(graph["nodes"][0]) == {"id", "kind", "canonical_name", "status", "domain"}
        assert graph["depth"] == 0
