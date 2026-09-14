"""Migration 0202 against real Postgres: the wipe takes the corpus and spares the keepers.

CI cannot catch a mistake in this migration. It runs against an EMPTY schema there, where
a wrong statement order never trips a foreign key and a statement that deletes too much
deletes nothing. The only place 0202 is ever exercised with rows in the tables is the
owner's box, once, irreversibly — so it is exercised here instead.

What this pins is the reason 0202 uses DELETE where the plan said
`TRUNCATE ... CASCADE`: TRUNCATE is STRUCTURAL, so it would have emptied every table
holding a foreign key into the wipe set regardless of what that key declares. The owner's
shopping list is exactly such a table (`list_items.source_note_id`, ON DELETE SET NULL) —
the column exists so a list outlives the note that spawned it, and a TRUNCATE would have
destroyed five of his list items on a box where the plan promised to keep them.
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
_PATH = BACKEND_ROOT / "migrations" / "versions" / "0202_reset_note_corpus.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0202_pg", _PATH)
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
    """Invoke `upgrade()` the way alembic does — a real connection bound to the `op`
    proxy — rather than reimplementing its SQL here, which would test the test.

    Connects as the SUPERUSER, like the deploy's `migrate` service: these tables FORCE
    row-level security, so a migration running as `jbrain_app` with no principal set
    would match zero rows and report success having deleted nothing."""
    dbname = database_url.rsplit("/", 1)[-1]
    engine = sqlalchemy.create_engine(_admin_url(_pg_cluster, dbname, driver="psycopg"))

    def _run() -> None:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                _migration().upgrade()

    yield _run
    engine.dispose()


async def _principal(maker: async_sessionmaker) -> str:
    """Mint the owner. The template database seeds no principal, and `lists` and
    `agent_sessions` both FK one — so a test that skipped this would be asserting against
    rows it never managed to insert."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        pid = (
            await s.execute(
                text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
            )
        ).scalar_one()
    return str(pid)


async def _count(maker: async_sessionmaker, sql: str, **params: object) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def test_the_wipe_spares_a_list_item_and_blanks_its_note(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """The defect `TRUNCATE ... CASCADE` would have shipped.

    A list item carrying `source_note_id` is the shape the plan promises to keep and
    TRUNCATE would have taken. It must survive the wipe with the column NULLed — which is
    what its own ON DELETE SET NULL says should happen, and what DELETE honours."""
    principal = await _principal(maker)
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"wipe-{uuid.uuid4()}", domain="general", destination=None, body="milk"
    )
    list_id, item_id = uuid.uuid4(), uuid.uuid4()
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.lists (id, principal_id, domain_code, title)"
                " VALUES (:id, :p, 'general', 'Groceries')"
            ),
            {"id": list_id, "p": principal},
        )
        await s.execute(
            text(
                "INSERT INTO app.list_items (id, list_id, body, source_note_id)"
                " VALUES (:id, :list, 'milk', :note)"
            ),
            {"id": item_id, "list": list_id, "note": note.id},
        )

    run_upgrade()  # type: ignore[operator]

    assert await _count(maker, "SELECT count(*) FROM app.notes WHERE id = :n", n=note.id) == 0
    # The item survives, and so does the list it belongs to.
    assert await _count(maker, "SELECT count(*) FROM app.list_items WHERE id = :i", i=item_id) == 1
    assert await _count(maker, "SELECT count(*) FROM app.lists WHERE id = :i", i=list_id) == 1
    orphaned = await _count(
        maker,
        "SELECT count(*) FROM app.list_items WHERE id = :i AND source_note_id IS NULL",
        i=item_id,
    )
    assert orphaned == 1, "the note link is blanked, not the row deleted"


async def test_the_wipe_takes_a_note_thread_and_leaves_ordinary_chat(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """ "Conversation history" is the owner's CHAT, which stays. A note's ingestion thread
    is not chat — it belongs to the note being deleted, and its `agent_sessions` row can
    only be identified while `note_conversations` still exists to name it. That ordering
    is the one thing in 0202 that cannot be recovered by re-running it."""
    principal = await _principal(maker)
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"wipe-{uuid.uuid4()}", domain="general", destination=None, body="guppy"
    )
    chat_id, thread_id = uuid.uuid4(), uuid.uuid4()
    async with scoped_session(maker, OWNER) as s:
        for sid in (chat_id, thread_id):
            await s.execute(
                text(
                    "INSERT INTO app.agent_sessions (id, principal_id, domain_scopes)"
                    " VALUES (:id, :p, ARRAY['general'])"
                ),
                {"id": sid, "p": principal},
            )
        await s.execute(
            text(
                "INSERT INTO app.note_conversations (session_id, note_id, state, note_body_sha)"
                " VALUES (:s, :n, 'settled', 'sha')"
            ),
            {"s": thread_id, "n": note.id},
        )

    run_upgrade()  # type: ignore[operator]

    assert (
        await _count(maker, "SELECT count(*) FROM app.agent_sessions WHERE id = :i", i=chat_id) == 1
    )
    gone = await _count(maker, "SELECT count(*) FROM app.agent_sessions WHERE id = :i", i=thread_id)
    assert gone == 0, "the note thread's session goes with the note"
    assert await _count(maker, "SELECT count(*) FROM app.note_conversations") == 0


async def test_the_wipe_runs_clean_on_an_already_empty_corpus(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """Idempotent, and safe in the order it is written. Running it twice must not raise:
    the deploy can retry `migrate`, and a statement ordered after its own parent would
    only surface here — CI runs this migration against an empty schema, where no foreign
    key is ever populated enough to complain."""
    run_upgrade()  # type: ignore[operator]
    run_upgrade()  # type: ignore[operator]
    assert await _count(maker, "SELECT count(*) FROM app.notes") == 0
    assert await _count(maker, "SELECT count(*) FROM app.facts") == 0


def test_the_downgrade_refuses_rather_than_lying() -> None:
    """A `downgrade()` that silently did nothing would read, to someone under pressure,
    as a rollback that worked. The rows are gone; the backup is the way back."""
    with pytest.raises(RuntimeError, match="no downgrade"):
        _migration().downgrade()
