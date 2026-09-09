"""Every proposal kind the source constructs must be one the CHECK admits.

Found live on 2026-09-09: `app.proposals`'s CHECK admitted ten kinds, while
`mergetools.propose_merge`, `externaltools`' library-video removal and `researchtools`'
report removal each staged a kind that was not among them. All three raised at stage time
on the real box, for months, because every test that covers them substitutes a fake repo —
so the constraint they violate was never once exercised.

The kinds are read out of the source with `ast` rather than restated here, because a list
restated in a test is the same failure one layer up: it goes stale the moment someone adds
a tool, which is exactly what happened. A new `ProposalSpec(kind=...)` anywhere in the
package fails this test until its migration lands.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_SRC = Path(__file__).resolve().parents[2] / "src" / "jbrain"


def _staged_kinds() -> set[str]:
    """Every literal `kind=` on a `ProposalSpec(...)` construction in the package.

    A dynamic kind (a variable, an f-string) is invisible here and deliberately so: this
    guards the common case without pretending to be exhaustive. `PREFS_KIND`-style module
    constants resolve because they are compared against the CHECK by their own suite.
    """
    kinds: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name != "ProposalSpec":
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "kind"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    kinds.add(kw.value.value)
    return kinds


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> SessionContext:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return SessionContext(principal_id=str(pid), principal_kind="owner", domain_scopes=("general",))


async def test_the_source_stages_kinds_the_check_actually_admits(
    maker: async_sessionmaker,
) -> None:
    owner = await _owner(maker)
    kinds = _staged_kinds()
    # A guard on the guard: if the AST walk silently stops finding call sites, this test
    # would pass by examining nothing.
    assert len(kinds) >= 8, f"expected the package to stage many kinds, found {sorted(kinds)}"

    refused: list[str] = []
    for kind in sorted(kinds):
        async with scoped_session(maker, owner) as session:
            try:
                await session.execute(
                    text(
                        "INSERT INTO app.proposals (principal_id, kind, title, domain_code)"
                        " VALUES (cast(:pid AS uuid), :kind, :title, 'general')"
                    ),
                    {"pid": owner.principal_id, "kind": kind, "title": f"probe {uuid.uuid4()}"},
                )
            except Exception as exc:  # noqa: BLE001 — the point is which kinds fail, not how
                if "proposals_kind_check" not in str(exc):
                    raise
                refused.append(kind)
    assert not refused, (
        f"these kinds are staged by the code and refused by the CHECK: {refused}."
        " Widen proposals_kind_check in a migration."
    )
