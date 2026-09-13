"""The triage sweep reading the owner's archivist-memory corrections against real
Postgres (the owner-only `archivist_memory` table, migration 0094).

The interactive archivist writes a TRIAGE CLARIFICATIONS section under the owner
principal; the unattended sweep runs under SYSTEM_CTX, resolves that principal, reads
the section, and injects it into every classification's system prompt. Gmail and the
LLM are faked — only the memory read crosses into the database.
"""

import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.gmail.client import GmailMessage
from jbrain.gmail.fake import FakeGmail
from jbrain.gmail.triage import InboxTriage
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.models.archivist import ArchivistMemoryRepo
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


async def _owner_principal(maker: async_sessionmaker) -> str:
    """Rotate the owner key and return the principal it minted — the ACTIVE one, so a
    second call (a rotation mid-test) returns the new owner and not a superseded row."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(
                text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
            )
        ).scalar()
    return str(pid)


def _msg() -> GmailMessage:
    return GmailMessage(
        id="m1",
        thread_id="t1",
        sender="news@acme.com",
        to="me@x",
        subject="What's new at Acme",
        date="Wed, 25 Jun 2026 09:00:00 +0000",
        snippet="updates",
        body="Acme product updates for June.",
    )


async def _factory(fake: FakeGmail):
    return fake


def _router() -> tuple[LlmRouter, FakeLlmClient]:
    fake = FakeLlmClient(responses=[json.dumps({"bucket": "spam", "confidence": 0.9})])
    return LlmRouter({"xai": fake}, {"triage.classify": ("xai", "m")}), fake


async def test_owner_memory_corrections_reach_the_classifier(maker: async_sessionmaker) -> None:
    owner = await _owner_principal(maker)
    # The interactive archivist persists a corrections section under the owner principal.
    owner_ctx = SessionContext(principal_id=owner, principal_kind="owner")
    async with scoped_session(maker, owner_ctx) as s:
        await ArchivistMemoryRepo().write(
            s,
            owner,
            "Taxonomy: Finance/Chase.\n"
            "=== TRIAGE CLARIFICATIONS ===\n"
            "- newsletters from acme.com are spam\n"
            "=== END TRIAGE CLARIFICATIONS ===\n",
        )

    fake = FakeGmail(messages=[_msg()])
    router, llm = _router()
    # The sweep is constructed with the app maker, exactly as the worker wires it.
    await InboxTriage(lambda: _factory(fake), router, maker).run({})

    # The correction (and only the marked section, not the taxonomy line) is injected.
    assert "Owner corrections" in llm.calls[0]["system"]
    assert "newsletters from acme.com are spam" in llm.calls[0]["system"]
    assert "Finance/Chase" not in llm.calls[0]["system"]


async def _write_memory(maker: async_sessionmaker, principal: str, content: str) -> None:
    ctx = SessionContext(principal_id=principal, principal_kind="owner")
    async with scoped_session(maker, ctx) as s:
        await ArchivistMemoryRepo().write(s, principal, content)


async def _clear_memory(maker: async_sessionmaker, principal: str) -> None:
    """`database_url` is module-scoped, so rows outlive a test. A rotation test turns on
    which document the sweep resolves, so it starts from a known-empty table."""
    ctx = SessionContext(principal_id=principal, principal_kind="owner")
    async with scoped_session(maker, ctx) as s:
        await s.execute(text("DELETE FROM app.archivist_memory"))


def _clarification(rule: str) -> str:
    return f"=== TRIAGE CLARIFICATIONS ===\n- {rule}\n=== END TRIAGE CLARIFICATIONS ===\n"


async def test_corrections_survive_an_owner_key_rotation(maker: async_sessionmaker) -> None:
    """The failure this guards, observed live: the owner rotated his key, so the sweep's
    unscoped `kind = 'owner'` lookup resolved a principal revoked months earlier with no
    memory row — every correction he recorded read back as "no corrections"."""
    old_owner = await _owner_principal(maker)
    await _clear_memory(maker, old_owner)
    await _write_memory(maker, old_owner, _clarification("newsletters from acme.com are spam"))

    new_owner = await _owner_principal(maker)  # a rotation: old principal revoked, new minted
    assert new_owner != old_owner

    fake = FakeGmail(messages=[_msg()])
    router, llm = _router()
    await InboxTriage(lambda: _factory(fake), router, maker).run({})

    # Resolved to the ACTIVE owner, and its memory carried the pre-rotation document.
    assert "newsletters from acme.com are spam" in llm.calls[0]["system"]


async def test_active_owners_corrections_win_over_a_revoked_owners(
    maker: async_sessionmaker,
) -> None:
    old_owner = await _owner_principal(maker)
    await _clear_memory(maker, old_owner)
    await _write_memory(maker, old_owner, _clarification("acme.com is spam"))
    new_owner = await _owner_principal(maker)
    await _write_memory(maker, new_owner, _clarification("acme.com is high"))

    fake = FakeGmail(messages=[_msg()])
    router, llm = _router()
    await InboxTriage(lambda: _factory(fake), router, maker).run({})

    assert "acme.com is high" in llm.calls[0]["system"]
    assert "acme.com is spam" not in llm.calls[0]["system"]


async def test_no_corrections_section_leaves_the_prompt_clean(maker: async_sessionmaker) -> None:
    owner = await _owner_principal(maker)
    owner_ctx = SessionContext(principal_id=owner, principal_kind="owner")
    async with scoped_session(maker, owner_ctx) as s:
        await ArchivistMemoryRepo().write(s, owner, "Taxonomy only, no triage section here.")

    fake = FakeGmail(messages=[_msg()])
    router, llm = _router()
    await InboxTriage(lambda: _factory(fake), router, maker).run({})

    assert "Owner corrections" not in llm.calls[0]["system"]
