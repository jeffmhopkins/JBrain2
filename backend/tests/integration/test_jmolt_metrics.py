"""jmolt's weekly metrics against real Postgres (§5 W4): the rubric computed from the
ledger, scratchpad, nights, and outbox, windowed to the last N days."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.jmolt_metrics import JmoltMetrics, format_report
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt import JmoltScratchRepo
from jbrain.models.jmolt_outbox import ActionLedgerRepo, OutboxRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401
from tests.unit.fakes import FakeSettingsStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _jmolt(pid: str) -> SessionContext:
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


def _admin(pid: str) -> SessionContext:
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


@pytest.fixture(autouse=True)
async def _clean(maker: async_sessionmaker) -> AsyncIterator[None]:
    async with scoped_session(maker, _admin("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_outbox"))
        await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
        await s.execute(text("DELETE FROM app.agent_sessions WHERE agent = 'jmolt'"))
    async with scoped_session(maker, _jmolt("")) as s:
        await s.execute(text("DELETE FROM app.jmolt_scratch"))
    yield


async def _owner_pid(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid)


async def test_metrics_count_actions_scratch_and_outbox(maker: async_sessionmaker) -> None:
    pid = await _owner_pid(maker)
    async with scoped_session(maker, _jmolt(pid)) as s:
        # Two distinct targets across three actions.
        await ActionLedgerRepo().record(s, pid, action="publish_comment", target="p1")
        await ActionLedgerRepo().record(s, pid, action="publish_comment", target="p2")
        await ActionLedgerRepo().record(s, pid, action="publish_vote", target="p1")
        await JmoltScratchRepo().write(s, pid, "index.md", "v1")
        await JmoltScratchRepo().write(s, pid, "index.md", "v2")  # a change → archived
        await OutboxRepo().stage(s, pid, kind="comment", payload={"post_id": "p1", "content": "x"})
    # A jmolt night (a session row).
    async with scoped_session(maker, _admin(pid)) as s:
        await s.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, agent, domain_scopes)"
                " VALUES (gen_random_uuid(), :pid, 'jmolt', ARRAY['jmolt'])"
            ),
            {"pid": pid},
        )

    store = FakeSettingsStore()
    m = await JmoltMetrics(maker=maker, settings_store=store).compute(days=7)

    assert m.nights_run == 1
    assert m.actions_by_type == {"publish_comment": 2, "publish_vote": 1}
    assert m.actions_total == 3
    assert m.distinct_targets == 2
    assert m.scratch_files == 1
    assert m.scratch_files_changed == 1  # one file changed this week (index.md)
    assert m.outbox_by_status == {"queued": 1}
    assert m.account_state == "ok"
    # The report renders without error and names the headline numbers.
    report = format_report(m)
    assert "Nights: 1 run" in report and "publish_comment: 2" in report


async def test_metrics_empty_when_nothing_happened(maker: async_sessionmaker) -> None:
    await _owner_pid(maker)
    m = await JmoltMetrics(maker=maker, settings_store=FakeSettingsStore()).compute(days=7)
    assert m.nights_run == 0 and m.actions_total == 0 and m.scratch_files == 0
    assert m.outbox_by_status == {}
