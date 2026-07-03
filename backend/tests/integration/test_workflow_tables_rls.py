"""Migration 0036 against real Postgres: RLS isolation for every workflow-engine
table (CLAUDE.md rule 3, docs/archive/WORKFLOW_ENGINE_PLAN.md E2).

Two postures are proven separately:
- domain-firewalled tables (`events`, `resolution_pin`): a health-domain row is
  invisible without the health scope — the analysis-table pattern;
- owner/system tables (`triggers`, `schedules`): visible to the owner context,
  invisible to any narrowed (owner_scoped) session — the agent_runs pattern;
- `pipelines` is global-read reference data (canonical_predicates precedent): every
  reader sees it; only the owner/system context writes it.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_ONLY = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
# A narrowed owner session (migration 0015): keeps owner identity but is firewalled
# to its domain_scopes, so an owner-only table is still visible while a
# domain-firewalled row outside scope is not.
OWNER_HEALTH = SessionContext(
    principal_id=str(uuid.uuid4()),
    principal_kind="owner",
    domain_scopes=("health",),
    owner_scoped=True,
)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed_health_workflow(maker: async_sessionmaker) -> dict[str, str]:
    """Insert one health-domain row in every domain-firewalled workflow table
    (plus the note/chunk/entity/principal a resolution_pin and event need); return
    ids. Fresh UUIDs per call so parametrized tests never collide."""
    ids = {name: str(uuid.uuid4()) for name in ("note", "chunk", "entity", "principal", "event")}
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.principals (id, kind, key_hash)"
                " VALUES (:id, 'capability_token', :kh)"
            ),
            {"id": ids["principal"], "kh": f"wf-{ids['principal']}"},
        )
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'health', 'BP 118/76 at Dr. Patel')"
            ),
            {"id": ids["note"], "cid": f"wf-{ids['note'][:13]}"},
        )
        await s.execute(
            text(
                "INSERT INTO app.chunks (id, note_id, domain_code, granularity, seq, text)"
                " VALUES (:id, :nid, 'health', 'paragraph', 0, 'BP 118/76 at Dr. Patel')"
            ),
            {"id": ids["chunk"], "nid": ids["note"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:id, 'Person', 'Dr. Patel', 'health')"
            ),
            {"id": ids["entity"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.events (id, type, domain_code, principal_id)"
                " VALUES (:id, 'note.created', 'health', :pid)"
            ),
            {"id": ids["event"], "pid": ids["principal"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.resolution_pin"
                " (note_id, chunk_id, occurrence_index, decision_kind, surface,"
                "  span_text_hash, entity_id, domain_code)"
                " VALUES (:nid, :cid, 0, 'identity', 'Dr. Patel', 'deadbeef',"
                "  :eid, 'health')"
            ),
            {"nid": ids["note"], "cid": ids["chunk"], "eid": ids["entity"]},
        )
    return ids


async def count_visible(
    maker: async_sessionmaker, ctx: SessionContext, query: str, params: dict[str, str]
) -> int:
    async with scoped_session(maker, ctx) as s:
        result = await s.execute(text(query), params)
        return result.scalar_one()


# (table, the WHERE that picks the seeded row, the id key into seed ids)
DOMAIN_TABLES = [
    ("events", "SELECT count(*) FROM app.events WHERE id = :id", "event"),
    (
        "resolution_pin",
        "SELECT count(*) FROM app.resolution_pin WHERE note_id = :id",
        "note",
    ),
]


@pytest.mark.parametrize(("table", "query", "id_key"), DOMAIN_TABLES)
async def test_workflow_domain_tables_enforce_firewall(
    maker: async_sessionmaker, table: str, query: str, id_key: str
) -> None:
    """A health-scoped workflow row is invisible without the health scope (rule 3)."""
    ids = await seed_health_workflow(maker)
    params = {"id": ids[id_key]}
    assert await count_visible(maker, HEALTH_ONLY, query, params) == 1
    assert await count_visible(maker, OWNER, query, params) == 1
    assert await count_visible(maker, GENERAL_ONLY, query, params) == 0
    assert await count_visible(maker, UNSCOPED, query, params) == 0


async def test_scoped_writer_cannot_smuggle_workflow_rows_across_domains(
    maker: async_sessionmaker,
) -> None:
    """A general-only writer cannot stamp a health domain on ANY firewalled row —
    events and resolution_pin (the content-bearing tables) both carry the same
    WITH CHECK (has_domain_scope(domain_code)), so the write firewall is exercised
    on each, not just one."""
    ids = await seed_health_workflow(maker)
    smuggles = [
        (
            "INSERT INTO app.events (id, type, domain_code)"
            " VALUES (gen_random_uuid(), 'sneak', 'health')",
            {},
        ),
        (
            "INSERT INTO app.resolution_pin (note_id, chunk_id, occurrence_index,"
            " decision_kind, normalized_predicate, domain_code)"
            " VALUES (:nid, :cid, 0, 'predicate_key', 'spouse', 'health')",
            {"nid": ids["note"], "cid": ids["chunk"]},
        ),
    ]
    for stmt, params in smuggles:
        with pytest.raises(ProgrammingError):
            async with scoped_session(maker, GENERAL_ONLY) as s:
                await s.execute(text(stmt), params)


async def seed_owner_definitions(maker: async_sessionmaker) -> dict[str, str]:
    """Insert one row in every owner/system definition table; return ids."""
    ids = {name: str(uuid.uuid4()) for name in ("schedule", "trigger")}
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.schedules (id, interval_seconds, next_run_at)"
                " VALUES (:id, 86400, now())"
            ),
            {"id": ids["schedule"]},
        )
        await s.execute(
            text(
                "INSERT INTO app.triggers (id, on_schedule_id, pipeline)"
                " VALUES (:id, :sid, 'nightly_sweep')"
            ),
            {"id": ids["trigger"], "sid": ids["schedule"]},
        )
    return ids


OWNER_TABLES = [
    ("schedules", "schedule"),
    ("triggers", "trigger"),
]


@pytest.mark.parametrize(("table", "id_key"), OWNER_TABLES)
async def test_workflow_owner_tables_are_owner_only(
    maker: async_sessionmaker, table: str, id_key: str
) -> None:
    """Definition/audit metadata carries no domain_code: visible to ANY owner
    session (narrowed or not — `is_owner()` is true for a domain-scoped owner, the
    bare-`is_owner()` agent_runs precedent), hidden from non-owner token sessions."""
    ids = await seed_owner_definitions(maker)
    query = f"SELECT count(*) FROM app.{table} WHERE id = :id"
    params = {"id": ids[id_key]}
    assert await count_visible(maker, OWNER, query, params) == 1
    # A domain-narrowed owner is still an owner (is_owner() ignores owner_scoped),
    # exactly like app.agent_runs — these config/audit rows aren't domain data.
    assert await count_visible(maker, OWNER_HEALTH, query, params) == 1
    assert await count_visible(maker, HEALTH_ONLY, query, params) == 0
    assert await count_visible(maker, UNSCOPED, query, params) == 0


async def test_pipelines_are_global_read_owner_write(maker: async_sessionmaker) -> None:
    """pipelines is reference data: every reader resolves a definition, only the
    owner/system context writes one (canonical_predicates precedent)."""
    name = f"pipe-{uuid.uuid4().hex[:8]}"
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("INSERT INTO app.pipelines (name, version, steps) VALUES (:n, 1, '[]')"),
            {"n": name},
        )
    query = "SELECT count(*) FROM app.pipelines WHERE name = :n"
    params = {"n": name}
    assert await count_visible(maker, OWNER, query, params) == 1
    assert await count_visible(maker, HEALTH_ONLY, query, params) == 1
    assert await count_visible(maker, GENERAL_ONLY, query, params) == 1
    assert await count_visible(maker, UNSCOPED, query, params) == 1
    # A non-owner write is refused by the owner-only INSERT policy.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, HEALTH_ONLY) as s:
            await s.execute(text("INSERT INTO app.pipelines (name, version) VALUES ('sneaky', 1)"))
