"""The read_note tool against real Postgres: a narrowed agent session reads an
in-scope note but cannot reach one outside its scope — the owner_scoped firewall
(P4.3) flowing end-to-end through a tool handler."""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.loop import ToolContext
from jbrain.agent.readtools import build_entity_handlers, build_read_handlers
from jbrain.agent.session import read_context
from jbrain.analysis.repo import SqlAnalysisRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _NoSearch:
    async def search(self, ctx, q, domain, limit):  # noqa: ANN001 - unused by read_note
        raise AssertionError("search not exercised here")


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


async def test_read_note_handler_respects_session_scope(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    run = uuid.uuid4().hex[:8]
    ids: dict[str, str] = {}
    async with scoped_session(maker, owner) as session:
        for code in ("health", "finance"):
            ids[code] = str(uuid.uuid4())
            await session.execute(
                text(
                    "INSERT INTO app.notes (id, client_id, domain_code, body)"
                    " VALUES (:id, :cid, :code, :body)"
                ),
                {"id": ids[code], "cid": f"{run}-{code}", "code": code, "body": f"{code} body"},
            )

    handlers = build_read_handlers(_NoSearch(), SqlNotesRepo(maker))  # type: ignore[arg-type]
    narrowed = ToolContext(
        session=read_context(owner.principal_id, ("health",)), scopes=("health",)
    )

    in_scope = await handlers["read_note"]({"note_id": ids["health"]}, narrowed)
    assert "health body" in in_scope

    # The finance note is invisible to a health-scoped session — RLS, not the tool.
    out_of_scope = await handlers["read_note"]({"note_id": ids["finance"]}, narrowed)
    assert "in scope" in out_of_scope


async def test_read_entity_handler_respects_session_scope(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    eid = str(uuid.uuid4())
    async with scoped_session(maker, owner) as session:
        await session.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                " VALUES (:id, 'Person', 'Aunt May', 'confirmed', 'health')"
            ),
            {"id": eid},
        )

    tools = build_entity_handlers(SqlAnalysisRepo(maker))
    health = ToolContext(session=read_context(owner.principal_id, ("health",)), scopes=("health",))
    assert "Aunt May [Person]" in await tools["read_entity"]({"entity_id": eid}, health)

    # A finance-scoped session cannot reach the health entity — RLS, not the tool.
    finance = ToolContext(
        session=read_context(owner.principal_id, ("finance",)), scopes=("finance",)
    )
    assert "in scope" in await tools["read_entity"]({"entity_id": eid}, finance)


async def test_relate_anchors_on_me_and_respects_the_firewall(maker: async_sessionmaker) -> None:
    owner = await _owner(maker)
    run = uuid.uuid4().hex[:8]
    me, spouse, note = (str(uuid.uuid4()) for _ in range(3))
    subject = str(uuid.uuid4())
    async with scoped_session(maker, owner) as session:
        # The "Me" anchor lives in general; the spouse and the edge linking them
        # live in health — so a session without health cannot traverse the bond.
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:id, 'Me', 'person')"),
            {"id": subject},
        )
        await session.execute(
            text(
                "INSERT INTO app.entities"
                " (id, kind, canonical_name, status, subject_id, domain_code)"
                " VALUES (:id, 'Person', 'Me', 'confirmed', :sub, 'general')"
            ),
            {"id": me, "sub": subject},
        )
        await session.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, status, domain_code)"
                " VALUES (:id, 'Person', 'Renata Kwon', 'confirmed', 'health')"
            ),
            {"id": spouse},
        )
        await session.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:id, :cid, 'health', 'my wife Renata')"
            ),
            {"id": note, "cid": f"{run}-n"},
        )
        await session.execute(
            text(
                "INSERT INTO app.facts"
                " (id, entity_id, predicate, kind, statement, assertion, object_entity_id,"
                "  reported_at, note_id, extractor, prompt_version, domain_code, status)"
                " VALUES (gen_random_uuid(), :me, 'spouse', 'relationship', 'married to Renata',"
                "  'asserted', :spouse, now(), :note, 'test', 'v1', 'health', 'active')"
            ),
            {"me": me, "spouse": spouse, "note": note},
        )

    tools = build_entity_handlers(SqlAnalysisRepo(maker))

    # With health in scope, "my wife" anchors on Me and follows the spouse edge.
    full = ToolContext(
        session=read_context(owner.principal_id, ("general", "health")),
        scopes=("general", "health"),
    )
    found = await tools["relate"]({"relationship": "wife"}, full)
    assert "Renata Kwon" in found

    # Without health, the spouse edge is invisible — no cross-firewall leak.
    general = ToolContext(
        session=read_context(owner.principal_id, ("general",)), scopes=("general",)
    )
    blocked = await tools["relate"]({"relationship": "wife"}, general)
    assert "Renata" not in blocked and "No 'wife' relationship" in blocked
