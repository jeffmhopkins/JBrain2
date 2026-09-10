"""Migration 0187 against real Postgres: the orphaned-memory repair actually runs.

The unit test pins the fold decision; this pins the half only a database can show — that
`upgrade()` resolves the ACTIVE owner, reads across principals, writes the folded document
back, and leaves the superseded rows untouched so a bad fold costs nothing.

The template database is already at head, so the migration has run (as a no-op on an empty
table). These tests seed the pre-rotation state and re-invoke `upgrade()` against a live
connection through alembic's own operations context.
"""

import importlib.util
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
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.archivist import ArchivistMemoryRepo
from tests.conftest import docker_available
from tests.integration.test_rls import _admin_url, _Cluster, _pg_cluster, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_PATH = BACKEND_ROOT / "migrations" / "versions" / "0187_archivist_memory_consolidate.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0187_pg", _PATH)
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
    """Invoke the migration's `upgrade()` the way alembic does — a real connection bound
    to the `op` proxy — rather than reimplementing its SQL in the test.

    Connects as the SUPERUSER, like the deploy's `migrate` service: `archivist_memory`
    FORCEs row-level security, so a migration running as `jbrain_app` with no
    `app.principal_kind` set would read zero rows and silently do nothing."""
    dbname = database_url.rsplit("/", 1)[-1]
    engine = sqlalchemy.create_engine(_admin_url(_pg_cluster, dbname, driver="psycopg"))

    def _run() -> None:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                _migration().upgrade()

    yield _run
    engine.dispose()


async def _rotate_to_new_owner(maker: async_sessionmaker) -> str:
    """Mint an owner principal (revoking any predecessor) and return the ACTIVE id."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(
                text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
            )
        ).scalar()
    return str(pid)


async def _write(maker: async_sessionmaker, principal: str, content: str) -> None:
    ctx = SessionContext(principal_id=principal, principal_kind="owner")
    async with scoped_session(maker, ctx) as session:
        await ArchivistMemoryRepo().write(session, principal, content)


async def _read(maker: async_sessionmaker, owner: str, principal: str) -> str:
    """Read one specific row, bypassing the repo's carry-forward fallback."""
    ctx = SessionContext(principal_id=owner, principal_kind="owner")
    async with scoped_session(maker, ctx) as session:
        row = (
            await session.execute(
                text("SELECT content FROM app.archivist_memory WHERE principal_id = :p"),
                {"p": principal},
            )
        ).scalar()
    return row or ""


async def _clear(maker: async_sessionmaker, owner: str) -> None:
    ctx = SessionContext(principal_id=owner, principal_kind="owner")
    async with scoped_session(maker, ctx) as session:
        await session.execute(text("DELETE FROM app.archivist_memory"))


async def test_orphaned_notes_are_folded_into_the_active_owners_document(
    maker: async_sessionmaker, run_upgrade
) -> None:
    """The owner's box, reproduced: notes written before a rotation, a bare corrections
    section written after it, and the notes stranded under the revoked principal."""
    old_owner = await _rotate_to_new_owner(maker)
    await _clear(maker, old_owner)
    await _write(maker, old_owner, "taxonomy: Finance/Chase")

    new_owner = await _rotate_to_new_owner(maker)
    clarifications = (
        "=== TRIAGE CLARIFICATIONS ===\n- acme.com → high\n=== END TRIAGE CLARIFICATIONS ==="
    )
    await _write(maker, new_owner, clarifications)

    run_upgrade()

    merged = await _read(maker, new_owner, new_owner)
    assert merged.startswith("=== TRIAGE CLARIFICATIONS ===")  # the live section still leads
    assert "taxonomy: Finance/Chase" in merged
    # The superseded row is left exactly as it was — the original is still recoverable.
    assert await _read(maker, new_owner, old_owner) == "taxonomy: Finance/Chase"


async def test_a_single_owner_with_one_document_is_untouched(
    maker: async_sessionmaker, run_upgrade
) -> None:
    owner = await _rotate_to_new_owner(maker)
    await _clear(maker, owner)
    await _write(maker, owner, "taxonomy: Finance/Chase")

    run_upgrade()

    assert await _read(maker, owner, owner) == "taxonomy: Finance/Chase"
