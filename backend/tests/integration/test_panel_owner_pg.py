"""Which owner the desk panel's turns run as, on a box with more than one owner principal.

The live box carried two `kind='owner'` rows (an owner-key rotation revokes the old row and mints
a new one) and 0.0.11's `WHERE kind='owner' LIMIT 1` met the old one first: the panel's "Desk
panel" threads were filed under a principal the PWA no longer logs in as (2026-10-12). Pinned
here against real Postgres: the panel's thread lands under the principal the PWA's login
authenticates as — over a revoked row AND over a newer un-revoked row nobody logs in with — the
repair migration and the runtime both move a misfiled thread (and its Proposals) to it, and none
of it is reachable from a panel's own RLS context.

Its own module, so its database is its own: the extra owner rows would otherwise change who
"the owner" is for every other test in a shared one.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import AgentSessionRepo
from jbrain.api import agent as agent_api
from jbrain.api import panel_agent
from jbrain.auth import keys, service
from jbrain.auth.owner import OWNER_PRINCIPAL_SQL, owner_principal_id
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, panel_context, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

Maker = async_sessionmaker[AsyncSession]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _migration() -> Any:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0234_panel_threads_to_live_owner.py"
    )
    spec = importlib.util.spec_from_file_location("m0234", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _pwa_owner(maker: Maker) -> str:
    """Rotate the owner key twice (the first row is left revoked, as on the live box), log the
    PWA in with the live key, and return the principal that login authenticates as."""
    repo = SqlAuthRepo(maker)
    await service.rotate_owner_key(repo)
    key = await service.rotate_owner_key(repo)
    token = await service.login(repo, key, "pwa")
    who = await service.authenticate(repo, token)
    assert who is not None and who.kind == "owner"
    return who.id


async def _owner_rows(maker: Maker) -> list[tuple[str, bool]]:
    async with scoped_session(maker, OWNER) as s:
        rows = await s.execute(
            text(
                "SELECT id::text, revoked_at IS NULL FROM app.principals"
                " WHERE kind = 'owner' ORDER BY created_at"
            )
        )
        return [(r[0], r[1]) for r in rows]


async def _stray_owner(maker: Maker) -> str:
    """A newer, UN-revoked owner row nobody logs in with — the shape a re-provision or a
    bootstrap artefact leaves. "The newest owner" would pick it; the PWA never uses it."""
    async with scoped_session(maker, SessionContext(auth_context="bootstrap")) as s:
        pid = (
            await s.execute(
                text(
                    "INSERT INTO app.principals (id, kind, key_hash, label)"
                    " VALUES (gen_random_uuid(), 'owner', :h, 'owner') RETURNING id::text"
                ),
                {"h": keys.hash_key(f"stray-{uuid.uuid4()}")},
            )
        ).scalar_one()
        await s.commit()
    return str(pid)


async def _desk(maker: Maker) -> PrincipalInfo:
    from jbrain.devices import service as devices
    from jbrain.devices.repo import SqlDeviceRepo

    made = await devices.provision_or_reflash(
        SqlDeviceRepo(maker),
        OWNER,
        f"panel desk-{uuid.uuid4().hex[:6]}",
        device_role="display",  # type: ignore[arg-type]
    )
    async with scoped_session(maker, OWNER) as s:
        pid = (
            await s.execute(
                text(
                    "SELECT id::text FROM app.principals WHERE subject_id = CAST(:s AS uuid)"
                    " AND revoked_at IS NULL"
                ),
                {"s": made.device.id},
            )
        ).scalar_one()
    return PrincipalInfo(id=pid, kind="device_key", label="desk", subject_id=made.device.id)


async def _session_owner(maker: Maker, sid: str) -> str:
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text(
                        "SELECT principal_id::text FROM app.agent_sessions"
                        " WHERE id = CAST(:i AS uuid)"
                    ),
                    {"i": sid},
                )
            ).scalar_one()
        )


async def test_the_panel_turn_runs_as_the_principal_the_pwa_logs_in_as(
    maker: Maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    pwa = await _pwa_owner(maker)
    rows = await _owner_rows(maker)
    assert len(rows) == 2 and rows[0][1] is False, "the old row is revoked, as on the live box"
    assert await owner_principal_id(maker) == pwa

    # Even an un-revoked newer row loses to the one the owner's PWA is actually signed in on.
    stray = await _stray_owner(maker)
    assert stray != pwa
    assert await owner_principal_id(maker) == pwa

    # The panel's real turn start: the thread it creates is filed under the PWA's principal.
    desk = await _desk(maker)
    sessions = AgentSessionRepo(maker)
    monkeypatch.setattr(agent_api, "get_agent_sessions", lambda request: sessions)
    asked: list[PrincipalInfo] = []

    async def chat(request: Any, principal: PrincipalInfo, body: Any) -> Any:
        asked.append(principal)
        return SimpleNamespace(headers={})

    monkeypatch.setattr(agent_api, "chat", chat)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=maker, live_turns={}))
    )
    started = await panel_agent.start_turn(request, desk, "jerv", "high tide?", fresh=False)  # type: ignore[arg-type]
    assert asked[0].id == pwa and started.owner_ctx.principal_id == pwa
    assert await _session_owner(maker, started.session_id) == pwa
    # The PWA's own session list (owner RLS, its login's principal) shows it.
    listed = await sessions.list(SessionContext(principal_id=pwa, principal_kind="owner"))
    assert started.session_id in {str(s.id) for s in listed}


async def test_a_misfiled_thread_is_rehomed_by_the_migration_and_by_the_runtime(
    maker: Maker,
) -> None:
    pwa = await owner_principal_id(maker)
    if pwa is None:
        pwa = await _pwa_owner(maker)
    old = next(pid for pid, live in await _owner_rows(maker) if pid != pwa)
    desk = await _desk(maker)
    sessions = AgentSessionRepo(maker)
    misfiled = SessionContext(principal_id=old, principal_kind="owner")

    async def misfile(agent: str) -> str:
        made = await sessions.create(misfiled, domain_scopes=[], title="Desk panel", agent=agent)
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_thread (subject_id, agent, agent_session_id)"
                    " VALUES (CAST(:s AS uuid), :a, CAST(:i AS uuid))"
                    " ON CONFLICT (subject_id, agent) DO UPDATE"
                    " SET agent_session_id = EXCLUDED.agent_session_id"
                ),
                {"s": desk.subject_id, "a": agent, "i": str(made.id)},
            )
            await s.execute(
                text(
                    "INSERT INTO app.proposals (session_id, principal_id, kind, title, domain_code)"
                    " VALUES (CAST(:i AS uuid), CAST(:p AS uuid), 'appointment', 't', 'general')"
                ),
                {"i": str(made.id), "p": old},
            )
            await s.commit()
        return str(made.id)

    async def proposal_owner(sid: str) -> str:
        async with scoped_session(maker, OWNER) as s:
            return str(
                (
                    await s.execute(
                        text(
                            "SELECT principal_id::text FROM app.proposals"
                            " WHERE session_id = CAST(:i AS uuid)"
                        ),
                        {"i": sid},
                    )
                ).scalar_one()
            )

    # The migration picks the same owner as the runtime, and moves the thread and its Proposal.
    m = _migration()
    async with scoped_session(maker, OWNER) as s:
        mig = (await s.execute(text(m.LIVE_OWNER))).scalar_one()
        run = (await s.execute(text(OWNER_PRINCIPAL_SQL))).scalar_one()
    assert str(mig) == str(run) == pwa
    jerv = await misfile("jerv")
    # A panel's own context can neither see nor move it: the repair is the owner's (RLS).
    async with scoped_session(maker, panel_context(desk.id, desk.subject_id)) as s:
        await s.execute(text(m.REHOME))
        await s.commit()
    assert await _session_owner(maker, jerv) == old, "a panel's context moves nothing"
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text(m.REHOME))
        await s.execute(text(m.REHOME))  # idempotent
        await s.commit()
    assert await _session_owner(maker, jerv) == pwa
    assert await proposal_owner(jerv) == pwa

    # A later rotation: the runtime moves the thread on the panel's next turn.
    dave = await misfile("minecraft_dave")
    owner = SessionContext(principal_id=pwa, principal_kind="owner")
    found = await panel_agent.panel_thread(
        maker, sessions, owner, desk.subject_id, "dave", fresh=False
    )
    assert found == dave
    assert await _session_owner(maker, dave) == pwa
    assert await proposal_owner(dave) == pwa
