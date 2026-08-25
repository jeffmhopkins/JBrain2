"""jmolt's history survives an owner-key rotation (docs/plans/JMOLT_PLAN.md).

The owner key rotates over a box's life, and each rotation REVOKES the old `owner`
principal and mints a new active one (auth.service.rotate_owner_key). jmolt's data is a
singleton filed under a STABLE anchor (the oldest owner principal, `agent/jmolt_owner.py`),
but the PWA authenticates as the CURRENT owner. The bug: the history endpoints filtered
jmolt's scratchpad/journal/ledger by the AUTHENTICATED (new) principal, so after a rotation
the notebook read as empty even though jmolt's files were intact under the old anchor.

This drives the real FastAPI app across a rotation: seed jmolt data under the original
owner, rotate the key (so login authenticates as a brand-new owner principal), and assert
every history surface still resolves the stable anchor and returns jmolt's data.
"""

from collections.abc import AsyncIterator

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

from jbrain.agent.jmolt_owner import jmolt_owner_principal_id
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.main import create_app
from jbrain.models.jmolt import JmoltJournalRepo, JmoltScratchRepo
from jbrain.models.jmolt_outbox import ActionLedgerRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _jmolt_ctx(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


async def _oldest_owner(maker: async_sessionmaker[AsyncSession]) -> str:
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (
            await s.execute(
                text("SELECT id FROM app.principals WHERE kind='owner' ORDER BY created_at LIMIT 1")
            )
        ).scalar()
    return str(pid)


async def test_history_survives_owner_key_rotation(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # 1) First owner: jmolt writes its data under this (the anchor).
    await service.rotate_owner_key(SqlAuthRepo(maker))
    pid_old = await _oldest_owner(maker)
    async with scoped_session(maker, _jmolt_ctx(pid_old)) as s:
        await JmoltScratchRepo().write(
            s, pid_old, "intro.md", "who I am: a naturalist among agents"
        )
        await ActionLedgerRepo().record(s, pid_old, action="web_fetch", target="https://example/x")
        await JmoltJournalRepo().add(s, pid_old, "quiet night, mostly read")

    # 2) The owner rotates their key: pid_old is REVOKED, a new active owner is minted.
    key_new = await service.rotate_owner_key(SqlAuthRepo(maker))

    # The stable anchor is still the OLDEST owner (where jmolt's data lives), not the new one.
    assert await jmolt_owner_principal_id(maker) == pid_old
    assert (await _oldest_owner(maker)) == pid_old  # unchanged by the rotation

    # 3) The PWA logs in as the NEW owner and must still see jmolt's history.
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        assert client.post("/api/auth/session", json={"owner_key": key_new}).status_code == 204
        base = "/api/settings/moltbook"

        files = client.get(f"{base}/files").json()
        assert [f["filename"] for f in files] == ["intro.md"]  # NOT an empty notebook

        content = client.get(f"{base}/files/content", params={"filename": "intro.md"}).json()
        assert "naturalist among agents" in content["content"]

        history = client.get(f"{base}/files/history", params={"filename": "intro.md"}).json()
        assert len(history) >= 1 and history[0]["filename"] == "intro.md"

        actions = client.get(f"{base}/actions").json()
        assert len(actions) == 1 and actions[0]["action"] == "web_fetch"

        journal = client.get(f"{base}/journal").json()
        assert [e["content"] for e in journal] == ["quiet night, mostly read"]
