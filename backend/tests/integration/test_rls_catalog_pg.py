"""Catalog-wide RLS coverage (SECURITY round 2).

Per-table isolation tests prove a policy does what it says; nothing proved every table
HAS one. A new table that forgets `ENABLE`/`FORCE ROW LEVEL SECURITY` (or enables it with
no policy, which reads as empty to the app role and hides the mistake) is the gap this
closes: it fails here the day it lands.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# Tables that legitimately carry no RLS: neither holds user data and the app role needs
# them readable. `alembic_version` is the migration head; `spatial_ref_sys` is PostGIS's
# coordinate-system reference table, owned by the extension.
_EXEMPT = {("public", "alembic_version"), ("public", "spatial_ref_sys")}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_every_table_forces_rls_with_a_policy(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, UNSCOPED) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT n.nspname AS schema, c.relname AS name,"
                    "   c.relrowsecurity AS enabled, c.relforcerowsecurity AS forced,"
                    "   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname IN ('app', 'public') AND c.relkind IN ('r', 'p')"
                )
            )
        ).all()
    assert len(rows) > 100  # the catalog query really saw the schema
    bad = sorted(
        f"{r.schema}.{r.name} (enabled={r.enabled} forced={r.forced} policies={r.policies})"
        for r in rows
        if (r.schema, r.name) not in _EXEMPT and not (r.enabled and r.forced and r.policies >= 1)
    )
    assert bad == []
    # The exemptions are real tables, so the list cannot rot into covering nothing.
    assert {(r.schema, r.name) for r in rows} >= _EXEMPT
