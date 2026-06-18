"""`save_place` (#13) end to end against real Postgres — the WRITE tool that
never writes.

The load-bearing #7/#9 proof: `save_place` STAGES a place-note Proposal (it does
NOT touch the graph, a `geofence` fact, or the `place_geofence` mirror), and only
the OWNER APPROVING it — driving the existing add_note executor → ingest →
extraction → `project_place_geofences` — produces a Place entity + a `geofence`
fact + the mirror row. So the place is authored by an owner-approved NOTE, exactly
as the doctrine requires.

Both model calls (note.extract, integrate.note) are faked with scripted JSON
standing in for what the extractor would read off the staged note body. RLS:
staging is refused for a narrowed/non-owner session.
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.locationtools import build_location_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.proposaltools import agent_note_executor
from jbrain.analysis.pipeline import AnalysisPipeline
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.ingest.pipeline import IngestPipeline
from jbrain.llm import FakeLlmClient, LlmRouter
from jbrain.locations import LocationToolRefusal, SqlLocationRepo
from jbrain.models.analysis import Entity, Fact
from jbrain.notes.repo import SqlNotesRepo
from jbrain.queue import SYSTEM_CTX
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# The place name / coordinates the test fixes the owner at — the same shape the
# extractor would mine out of the staged note body.
_PLACE = "L6 Cabin"
_LAT, _LON, _RADIUS = 41.5, -72.5, 120


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner_ctx(maker: async_sessionmaker) -> SessionContext:
    """A real owner principal (the proposals FK requires one) + an owner context."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_kind="owner", principal_id=str(pid))


async def _owner_at_a_position(maker: async_sessionmaker) -> None:
    """Make the owner's own device resolvable AND give it a RECENT fix at the place,
    so `save_place` can anchor there. Mirrors the read-tool fixtures: a "Me" person
    entity hard-link → operatedBy → Device entity (subject_id set) → a fix at `now`."""
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, :l, 'device')"),
            {"s": sid, "l": "L6 owner phone subj"},
        )
        await session.execute(
            text(
                "INSERT INTO app.principals (id, kind, subject_id, key_hash)"
                " VALUES (:p, 'device_key', :s, :kh)"
            ),
            {"p": pid, "s": sid, "kh": uuid.uuid4().hex},
        )
        # "Me" hard-linked to a person subject (never a track — resolution must hop
        # Me → operatedBy → Device).
        me_subject = str(uuid.uuid4())
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:s, 'Me', 'person')"),
            {"s": me_subject},
        )
        me = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code, subject_id)"
                    " VALUES (gen_random_uuid(), 'Person', 'Me', 'location', cast(:s AS uuid))"
                    " RETURNING id::text"
                ),
                {"s": me_subject},
            )
        ).scalar_one()
        device = (
            await session.execute(
                text(
                    "INSERT INTO app.entities (id, kind, canonical_name, domain_code, subject_id)"
                    " VALUES (gen_random_uuid(), 'Device', 'L6 owner phone', 'location',"
                    "   cast(:s AS uuid)) RETURNING id::text"
                ),
                {"s": sid},
            )
        ).scalar_one()
        note_id = (
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (gen_random_uuid(), :cid, 'location', 'device note') RETURNING id"
                ),
                {"cid": f"opby-{device}"},
            )
        ).scalar()
        await session.execute(
            text(
                "INSERT INTO app.facts (id, entity_id, predicate, kind, assertion, status,"
                "   object_entity_id, domain_code, statement, reported_at, note_id, extractor,"
                "   prompt_version)"
                " VALUES (gen_random_uuid(), cast(:d AS uuid), 'operatedBy', 'relationship',"
                "   'asserted', 'active', cast(:m AS uuid), 'location', 'op', :now, :n, 't', 'v1')"
            ),
            {"d": device, "m": me, "now": datetime.now(UTC), "n": note_id},
        )
        await session.execute(
            text(
                "INSERT INTO app.location_fixes"
                " (subject_id, principal_id, captured_at, latitude, longitude)"
                " VALUES (:s, :p, :ts, :lat, :lon)"
            ),
            {"s": sid, "p": pid, "ts": datetime.now(UTC), "lat": _LAT, "lon": _LON},
        )


# The scripted extraction the faked model returns for the STAGED place note —
# a Place mention + a `geofence` fact carrying the exact schema shape
# ({center:{latitude,longitude}, radiusMeters}). Stands in for what grok would read
# off the note body `_place_note_body` composed.
_EXTRACT = json.dumps(
    {
        "title": _PLACE,
        "tags": ["place", "location"],
        "mentions": [{"name": _PLACE, "kind": "Place", "surface_text": _PLACE}],
        "facts": [
            {
                "entity_ref": _PLACE,
                "predicate": "geofence",
                "qualifier": "",
                "kind": "state",
                "statement": f"{_PLACE} is a saved geofence.",
                "value_json": {
                    "center": {"latitude": _LAT, "longitude": _LON},
                    "radiusMeters": _RADIUS,
                },
                "assertion": "asserted",
                "object_entity_ref": None,
                "domain": "location",
                "temporal": None,
            }
        ],
        "temporal_tokens": [],
    }
)
_INTENT = json.dumps(
    {
        "resolutions": [
            {"mention_ref": _PLACE, "mode": "new", "new_kind": "Place", "new_name": _PLACE}
        ],
        "facts": [
            {
                "entity_ref": _PLACE,
                "predicate": "geofence",
                "kind": "state",
                "assertion": "asserted",
                "statement": f"{_PLACE} is a saved geofence.",
                "value_json": {
                    "center": {"latitude": _LAT, "longitude": _LON},
                    "radiusMeters": _RADIUS,
                },
                "self_confidence": 0.95,
                "chunk_id": "x",
                "surface": _PLACE,  # present in the body → surface-attested → commit
            }
        ],
    }
)


def _pipeline(maker: async_sessionmaker) -> AnalysisPipeline:
    fake = FakeLlmClient(responses=[_EXTRACT, _INTENT])
    router = LlmRouter(
        {"xai": fake},
        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},
    )
    return AnalysisPipeline(maker, router)


async def _place_geofence_count(maker: async_sessionmaker) -> int:
    async with scoped_session(maker, OWNER) as session:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.place_geofence pg"
                    " JOIN app.entities e ON e.id = pg.place_entity_id"
                    " WHERE e.canonical_name = :n"
                ),
                {"n": _PLACE},
            )
        ).scalar() or 0


async def test_save_place_stages_then_approval_projects_place_geofence(
    maker: async_sessionmaker, tmp_path
) -> None:
    ctx = await _owner_ctx(maker)
    await _owner_at_a_position(maker)
    tool_ctx = ToolContext(session=ctx, scopes=())
    repo = ProposalRepo(maker)

    handlers = build_location_handlers(
        SqlLocationRepo(maker),
        SqlDeviceRepo(maker),
        SqlAnalysisRepo(maker),
        None,
        repo,
    )

    # 1) Stage. The tool returns a Proposal chip — and writes NO mirror row.
    out = await handlers["save_place"]({"name": _PLACE, "radius_m": _RADIUS}, tool_ctx)
    assert isinstance(out, ToolOutput) and out.proposal is not None
    prop_id = out.proposal.proposal_id
    assert await _place_geofence_count(maker) == 0  # nothing projected yet — staging only

    # The staged Proposal is pending, an add_note leaf carrying the place note body.
    proposal, nodes = await repo.load(ctx, prop_id)
    assert proposal.status == "staged" and proposal.kind == "knowledge"
    assert proposal.domain == "location"
    leaf = nodes[0]
    assert leaf.op == "add_note" and leaf.status == "pending"
    assert "geofence" in leaf.preview["body"] and "radiusMeters" in leaf.preview["body"]

    # 2) Owner approves + enacts → the existing add_note executor writes the note and
    # enqueues ingestion (the queue is faked; we drive ingest + integrate by hand).
    await repo.decide(ctx, leaf.id, approve=True)
    jobs = _RecordingJobs()
    executor = agent_note_executor(SqlNotesRepo(maker), jobs)  # type: ignore[arg-type]
    plan = await repo.enact(ctx, prop_id, executor)
    assert plan.enactable == (leaf.id,)
    assert await _place_geofence_count(maker) == 0  # the note exists; not yet analyzed

    note_id = jobs.enqueued[0][1]["note_id"]

    # 3) Drive the SHIPPED pipeline the note re-entered: ingest → extraction →
    # integration → project_place_geofences (Place-only projection already exists).
    await IngestPipeline(maker, FsBlobStore(tmp_path)).ingest_note({"note_id": note_id})
    await _pipeline(maker).integrate_note({"note_id": note_id})

    # 4) The owner-approved NOTE produced the Place entity, the geofence fact, AND the
    # mirror row — none of which the tool wrote directly (#7).
    async with scoped_session(maker, SYSTEM_CTX) as session:
        ent = (
            await session.execute(select(Entity).where(Entity.canonical_name == _PLACE))
        ).scalar_one()
        fact = (
            await session.execute(
                select(Fact).where(Fact.entity_id == ent.id, Fact.predicate == "geofence")
            )
        ).scalar_one()
        row = (
            await session.execute(
                text(
                    "SELECT radius_m, ST_Y(center::geometry) AS lat, ST_X(center::geometry) AS lon"
                    " FROM app.place_geofence WHERE place_entity_id = :e"
                ),
                {"e": str(ent.id)},
            )
        ).one()
    assert fact.status == "active"
    assert (row.radius_m, row.lat, row.lon) == (float(_RADIUS), _LAT, _LON)
    # The note that sourced it is the agent-authored proposal note (#7 provenance).
    assert str(fact.note_id) == note_id


async def test_save_place_refuses_a_narrowed_session_and_stages_nothing(
    maker: async_sessionmaker,
) -> None:
    # A narrowed (owner_scoped) session is not a full owner — the registration-time
    # wrapper refuses BEFORE any read or stage, so no Proposal is created.
    ctx = await _owner_ctx(maker)
    narrowed = SessionContext(
        principal_kind="owner",
        principal_id=ctx.principal_id,
        domain_scopes=("location",),
        owner_scoped=True,
    )
    repo = ProposalRepo(maker)
    handlers = build_location_handlers(
        SqlLocationRepo(maker), SqlDeviceRepo(maker), SqlAnalysisRepo(maker), None, repo
    )
    with pytest.raises(LocationToolRefusal):
        await handlers["save_place"]({"name": _PLACE}, ToolContext(session=narrowed, scopes=()))
    # Nothing staged for the narrowed session.
    full = SessionContext(principal_kind="owner", principal_id=ctx.principal_id)
    assert await repo.list_open(full) == []


class _RecordingJobs:
    """Records ingestion enqueues — the note write is real; the queue is faked."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, ctx: object, kind: str, payload: dict) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"
