"""Migration 0204 against real Postgres: the fork fold keeps the owner's thread.

CI runs migrations against an EMPTY schema, where a statement that deletes the wrong rows
deletes nothing and passes. This one only ever meets real rows on the owner's box, once —
so it meets them here instead.

The two halves that matter are opposites, and a fold that got either wrong would be
silent: the newer EMPTY fork must go (it is the one the note screen was pointing at), and
a newer fork that COMMITTED FACTS must stay, because deleting it strands those facts from
the conversation that vouches for them.
"""

import importlib.util
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from jbrain.models.note_conversation import NoteConversationRepo
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import (  # noqa: F401
    OWNER,
    _admin_url,
    _Cluster,
    _pg_cluster,
    database_url,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_PATH = BACKEND_ROOT / "migrations" / "versions" / "0204_fold_forked_note_conversations.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0204_pg", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def run_upgrade(_pg_cluster: _Cluster, database_url: str) -> Iterator[object]:  # noqa: F811
    """Alembic's own invocation path, as the SUPERUSER the deploy's `migrate` service
    uses — these tables FORCE row-level security, so a migration run as `jbrain_app`
    with no principal set matches zero rows and reports success having deleted nothing."""
    dbname = database_url.rsplit("/", 1)[-1]
    engine = sqlalchemy.create_engine(_admin_url(_pg_cluster, dbname, driver="psycopg"))

    def _run() -> None:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                _migration().upgrade()

    yield _run
    engine.dispose()


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text(
                        "SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL"
                    )
                )
            ).scalar_one()
        )


async def _note(maker: async_sessionmaker, body: str) -> str:
    """Through the repo, not a hand-built INSERT: `app.notes` carries defaults and a
    domain FK this test has no business restating."""
    note, _ = await SqlNotesRepo(maker).create_note(
        OWNER, client_id=f"fold-{uuid.uuid4()}", domain="general", destination=None, body=body
    )
    return note.id


async def _conversation(
    maker: async_sessionmaker,
    pid: str,
    note_id: str,
    *,
    minutes: int,
    wrote: bool = False,
) -> str:
    """One settled conversation on a note, `created_at` pushed out by `minutes` — the
    fold keys on that ordering, so it is stated rather than left to insertion luck.

    Built through `NoteConversationRepo`, not hand-written INSERTs: these tables carry
    NOT NULLs and column names this test has no business restating, and a fixture that
    restates them is a fixture that goes stale without anyone noticing."""
    sid = str(uuid.uuid4())
    repo = NoteConversationRepo()
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_sessions"
                " (id, principal_id, title, agent, domain_scopes)"
                " VALUES (CAST(:i AS uuid), CAST(:p AS uuid), 'a note', 'note_ingest',"
                "         ARRAY['general'])"
            ),
            {"i": sid, "p": pid},
        )
        await repo.start(s, session_id=sid, note_id=note_id, body_sha="0" * 64)
        await repo.record_tool_call(
            s,
            sid,
            name="close_reading",
            ok=True,
            fact_ids=[uuid.uuid4()] if wrote else [],
            domains=["general"] if wrote else [],
        )
        await repo.set_state(s, sid, "settled")
        await s.execute(
            text(
                "UPDATE app.note_conversations"
                "   SET created_at = created_at + make_interval(mins => :m)"
                " WHERE session_id = CAST(:s AS uuid)"
            ),
            {"s": sid, "m": minutes},
        )
    return sid


async def _alive(maker: async_sessionmaker, note_id: str) -> list[str]:
    async with scoped_session(maker, OWNER) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT session_id FROM app.note_conversations"
                    " WHERE note_id = CAST(:n AS uuid) ORDER BY created_at"
                ),
                {"n": note_id},
            )
        ).all()
    return [str(r.session_id) for r in rows]


async def test_the_fold_keeps_the_thread_the_owner_talked_in(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """His TV note, as it sits on the box: a first pass that resolved the entity and
    wrote the fact, then a fork born from his addition that resolved the entity and wrote
    NOTHING. The note screen shows the newest, so it showed the fork."""
    pid = await _owner_principal(maker)
    note_id = await _note(maker, 'My tv is 58"')
    mine = await _conversation(maker, pid, note_id, minutes=0, wrote=True)
    fork = await _conversation(maker, pid, note_id, minutes=6)

    run_upgrade()  # type: ignore[operator]

    assert await _alive(maker, note_id) == [mine], "the fold kept the wrong thread"
    # WHOLE, not the side row alone: a surviving session would keep the note's body in a
    # transcript nothing points at (`analysis/purge.py:_purge_conversations`).
    async with scoped_session(maker, OWNER) as s:
        left = (
            await s.execute(
                text("SELECT count(*) FROM app.agent_sessions WHERE id = CAST(:s AS uuid)"),
                {"s": fork},
            )
        ).scalar_one()
    assert left == 0


async def test_the_fold_refuses_to_delete_a_fork_that_committed_facts(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """The half that must NOT happen. A newer conversation that wrote is the provenance
    of the facts it wrote; deleting it strands them. A duplicate thread the owner can
    still reach is the smaller problem, so this one stays for a human."""
    pid = await _owner_principal(maker)
    note_id = await _note(maker, "the roof needs looking at")
    mine = await _conversation(maker, pid, note_id, minutes=0, wrote=True)
    wrote_too = await _conversation(maker, pid, note_id, minutes=6, wrote=True)

    run_upgrade()  # type: ignore[operator]

    assert await _alive(maker, note_id) == [mine, wrote_too]


async def test_the_fold_leaves_an_unforked_note_entirely_alone(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """The common case on the box: one note, one conversation, nothing to fold. A
    statement that deleted here would take the note's only thread."""
    pid = await _owner_principal(maker)
    note_id = await _note(maker, "I have an aquarium with a guppy in it")
    only = await _conversation(maker, pid, note_id, minutes=0)

    run_upgrade()  # type: ignore[operator]

    assert await _alive(maker, note_id) == [only]
