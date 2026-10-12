"""The desk panel's reach into the real agents, against real Postgres and the real app.

The one property that matters: a device key reaches the owner's Jerv or Dave ONLY when the
server says so — the panel was flashed as a `display` AND the owner switched `agent_access` on.
A twin's `jpet` panel never does, whatever it sends and even if the flag were set on its row;
a display the owner has not switched on keeps the tool-less persona; and no panel can switch
itself on. The agent turn itself is replaced by a recorder here (its own unit tests drive it):
what is under test is which door the request goes through.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.agent.session import AgentSessionRepo
from jbrain.api import endpoint as endpoint_api
from jbrain.api import panel_agent
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, panel_context, scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

Maker = async_sessionmaker[AsyncSession]
AUDIO = b"\x00\x01" * 1600


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.endpoint_panel_thread"))
        await s.execute(text("DELETE FROM app.endpoint_panel"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.commit()
    yield sessions
    await engine.dispose()


@dataclass
class Box:
    client: TestClient
    maker: Maker
    dbg: dict[str, str]
    keys: dict[str, str]
    devices: dict[str, str]
    turns: list[dict[str, Any]] = field(default_factory=list)

    def panel(self, who: str, method: str, path: str, **kw: Any) -> Any:
        # The owner cookie also satisfies `PanelDep` and would authenticate first.
        self.client.cookies.clear()
        return self.client.request(
            method, path, headers={"Authorization": f"Bearer {self.keys[who]}"}, **kw
        )

    def talk(self, who: str, query: str = "persona=jerv") -> Any:
        return self.panel(who, "POST", f"/api/endpoint/converse?{query}", content=AUDIO)

    def sql(self, statement: str, **params: object) -> None:
        async def run() -> None:
            async with scoped_session(self.maker, OWNER) as s:
                await s.execute(text(statement), params)
                await s.commit()

        asyncio.run(run())


class _Persona:
    """The tool-less persona's model: the old path answering is proof the gate stayed shut."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, task: str, **kw: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(text="Persona answer.")


@pytest.fixture
def box(
    database_url: str,  # noqa: F811
    maker: Maker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Box]:
    repo = SqlAuthRepo(maker)
    owner_key = asyncio.run(service.rotate_owner_key(repo))
    debug_key, _ = asyncio.run(service.mint_capability(repo, "claude", ttl_hours=1))
    settings = Settings(
        secure_cookies=False,
        database_url=database_url,
        debug_access_enabled=True,
        blob_dir=str(tmp_path),
        whisper_url="http://tts-stt:8080/v1",
        brain_tts_url="",
    )
    app = create_app(settings)
    turns: list[dict[str, Any]] = []

    async def heard(self: Any, audio: bytes, **kw: Any) -> Any:
        return SimpleNamespace(text="what is on my list")

    async def agent_turn(
        principal: PrincipalInfo,
        request: Any,
        audio: bytes,
        started: float,
        *,
        agent: str,
        voice: bool,
        fresh: bool,
        jt: int = 1,
        steps: bool = False,
    ) -> Any:
        turns.append({"device": principal.id, "agent": agent, "fresh": fresh, "jt": jt})
        return endpoint_api.Response(status_code=200, content=b"agent")

    monkeypatch.setattr(endpoint_api.WhisperCppClient, "transcribe", heard)
    monkeypatch.setattr(endpoint_api, "_agent_turn", agent_turn)
    with TestClient(app) as client:
        cast_app: FastAPI = app
        cast_app.state.llm_router = _Persona()
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        made = {
            name: client.post("/api/devices", json={"label": f"panel {name}", "device_role": role})
            for name, role in (("Jeff", "display"), ("Quorra", "jpet"), ("Tamsin", "jpet"))
        }
        keys = {name: r.json()["key"] for name, r in made.items()}
        dbg = {"Authorization": f"Bearer {debug_key}"}
        client.cookies.clear()
        listed = client.get("/api/debug/panel", headers=dbg).json()
        devices = {p["name"]: p["device_id"] for p in listed}
        yield Box(client, maker, dbg, keys, devices, turns)


def _owner_put(box: Box, who: str, on: bool) -> Any:
    return box.client.put(
        f"/api/debug/panel/{box.devices[who]}/agent", headers=box.dbg, json={"agent_access": on}
    )


# ---- the gate, through the route ------------------------------------------------------------


def test_a_display_the_owner_has_not_switched_on_keeps_the_persona(box: Box) -> None:
    r = box.talk("Jeff")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-jpanel-turn"
    assert box.turns == [], "no agent turn without the owner's switch"
    # 0.0.12 asks for JT2: the persona's answer comes as the same records, still no agent.
    streamed = box.talk("Jeff", "persona=jerv&jt=2")
    assert streamed.status_code == 200 and streamed.content.startswith(b"JT2")
    assert b"Persona answer." in streamed.content
    assert box.turns == []


def test_the_owner_s_switch_opens_the_real_agent_for_his_display(box: Box) -> None:
    assert _owner_put(box, "Jeff", True).json() == {"agent_access": True}
    assert box.talk("Jeff").content == b"agent"
    assert box.talk("Jeff", "persona=dave&new_thread=true").content == b"agent"
    assert box.talk("Jeff", "persona=jerv&jt=2").content == b"agent"
    assert [(t["agent"], t["fresh"], t["jt"]) for t in box.turns] == [
        ("jerv", False, 1),
        ("dave", True, 1),
        ("jerv", False, 2),
    ]
    assert {t["device"] for t in box.turns} == {box.devices["Jeff"]}

    assert _owner_put(box, "Jeff", False).json() == {"agent_access": False}
    box.talk("Jeff")
    assert len(box.turns) == 3, "switched off, the next talk is the persona again"


def test_a_twin_s_panel_never_reaches_the_agent_whatever_it_asks(box: Box) -> None:
    """Even with the flag forced on its row behind the API's back: the role is the second key."""
    assert _owner_put(box, "Quorra", True).status_code == 409
    box.sql(
        "INSERT INTO app.endpoint_panel (subject_id, agent_access)"
        " SELECT subject_id, true FROM app.principals WHERE id = CAST(:d AS uuid)",
        d=box.devices["Quorra"],
    )
    for query in (
        "persona=jerv",
        "persona=jerv&new_thread=true",
        "persona=jerv&jt=2",
        "persona=pet",
        "persona=pet&jt=2",
    ):
        assert box.talk("Quorra", query).content != b"agent"
    assert not box.talk("Quorra", "persona=pet&jt=2").content.startswith(b"JT2"), (
        "the pet path never streams"
    )
    assert box.talk("Quorra", "persona=dave").status_code == 403
    assert box.turns == []


def test_dave_is_refused_rather_than_faked_without_the_switch(box: Box) -> None:
    r = box.talk("Jeff", "persona=dave")
    assert r.status_code == 403
    assert box.turns == []


def test_an_unknown_persona_is_refused_at_the_edge(box: Box) -> None:
    assert box.talk("Jeff", "persona=admin").status_code == 422


def test_a_revoked_key_is_shut_out_before_the_gate(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    box.sql(
        "UPDATE app.principals SET revoked_at = now() WHERE id = CAST(:d AS uuid)",
        d=box.devices["Jeff"],
    )
    assert box.talk("Jeff").status_code == 401
    assert box.turns == []


def test_the_owner_routes_read_and_write_the_switch(box: Box) -> None:
    owner_key_cookie = box.client  # the session cookie is cleared by every panel call
    assert _owner_put(box, "Jeff", True).status_code == 200
    got = owner_key_cookie.get(f"/api/debug/panel/{box.devices['Jeff']}/agent", headers=box.dbg)
    assert got.json() == {"agent_access": True}
    assert (
        box.client.put(
            "/api/debug/panel/not-a-uuid/agent", headers=box.dbg, json={"agent_access": True}
        ).status_code
        == 404
    )
    assert (
        box.client.get(
            "/api/debug/panel/00000000-0000-0000-0000-000000000000/agent", headers=box.dbg
        ).status_code
        == 404
    )
    # A panel's key is not the owner and cannot reach the switch.
    r = box.panel(
        "Jeff",
        "PUT",
        f"/api/endpoint/panels/{box.devices['Jeff']}/agent",
        json={"agent_access": True},
    )
    assert r.status_code == 401


# ---- the siblings list ------------------------------------------------------------------------


def test_the_desk_panel_learns_the_twins_names_and_nothing_else(box: Box) -> None:
    r = box.panel("Jeff", "GET", "/api/endpoint/siblings")
    assert r.status_code == 200, r.text
    got = r.json()["siblings"]
    assert [s["name"] for s in got] == ["Quorra", "Tamsin"]
    assert {s["device_id"] for s in got} == {box.devices["Quorra"], box.devices["Tamsin"]}
    assert all(set(s) == {"device_id", "name"} for s in got)


def test_a_twin_s_panel_may_not_read_the_roster(box: Box) -> None:
    assert box.panel("Quorra", "GET", "/api/endpoint/siblings").status_code == 403


def test_a_revoked_twin_drops_out_of_the_list(box: Box) -> None:
    box.sql(
        "UPDATE app.principals SET revoked_at = now() WHERE id = CAST(:d AS uuid)",
        d=box.devices["Tamsin"],
    )
    got = box.panel("Jeff", "GET", "/api/endpoint/siblings").json()["siblings"]
    assert [s["name"] for s in got] == ["Quorra"]


# ---- the gate and the thread, directly -------------------------------------------------------


async def _device(maker: Maker, name: str, role: str) -> PrincipalInfo:
    from jbrain.devices import service as devices
    from jbrain.devices.repo import SqlDeviceRepo

    made = await devices.provision_or_reflash(SqlDeviceRepo(maker), OWNER, name, device_role=role)  # type: ignore[arg-type]
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
    return PrincipalInfo(id=pid, kind="device_key", label=name, subject_id=made.device.id)


async def _switch(maker: Maker, subject: str, on: bool) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.endpoint_panel (subject_id, agent_access)"
                " VALUES (CAST(:s AS uuid), :on) ON CONFLICT (subject_id)"
                " DO UPDATE SET agent_access = EXCLUDED.agent_access"
            ),
            {"s": subject, "on": on},
        )
        await s.commit()


async def test_the_gate_answers_from_the_server_s_rows_only(maker: Maker) -> None:
    desk = await _device(maker, "panel gate-desk", "display")
    twin = await _device(maker, "panel gate-twin", "jpet")
    assert not await panel_agent.agent_allowed(maker, desk), "no row: closed"
    await _switch(maker, desk.subject_id, False)
    assert not await panel_agent.agent_allowed(maker, desk), "switched off: closed"
    await _switch(maker, desk.subject_id, True)
    assert await panel_agent.agent_allowed(maker, desk)
    await _switch(maker, twin.subject_id, True)
    assert not await panel_agent.agent_allowed(maker, twin), "a pet panel never, flag or not"
    owner = PrincipalInfo(id=OWNER.principal_id or "", kind="owner", label="owner")
    assert not await panel_agent.agent_allowed(maker, owner), "the owner's cookie is not a panel"
    no_subject = PrincipalInfo(id=desk.id, kind="device_key", label="x")
    assert not await panel_agent.agent_allowed(maker, no_subject)
    # Another panel's key presented with the desk's subject: RLS shows it no row.
    forged = PrincipalInfo(id=twin.id, kind="device_key", label="x", subject_id=desk.subject_id)
    assert not await panel_agent.agent_allowed(maker, forged)


async def test_a_panel_cannot_switch_itself_on(maker: Maker) -> None:
    desk = await _device(maker, "panel gate-self", "display")
    await _switch(maker, desk.subject_id, False)
    async with scoped_session(maker, panel_context(desk.id, desk.subject_id)) as s:
        await s.execute(
            text(
                "UPDATE app.endpoint_panel SET agent_access = true"
                " WHERE subject_id = CAST(:s AS uuid)"
            ),
            {"s": desk.subject_id},
        )
        await s.commit()
    assert not await panel_agent.agent_allowed(maker, desk)


async def test_the_thread_is_the_owner_s_alone(maker: Maker) -> None:
    desk = await _device(maker, "panel thread-rls", "display")
    sessions = AgentSessionRepo(maker)
    owner = SessionContext(principal_id=await _owner_id(maker), principal_kind="owner")
    sid = await panel_agent.panel_thread(
        maker, sessions, owner, desk.subject_id, "jerv", fresh=False
    )
    async with scoped_session(maker, panel_context(desk.id, desk.subject_id)) as s:
        seen = (
            await s.execute(text("SELECT count(*) FROM app.endpoint_panel_thread"))
        ).scalar_one()
    assert seen == 0, "a panel does not even see which session it lands in"
    async with scoped_session(maker, panel_context(desk.id, desk.subject_id)) as s:
        with pytest.raises(DBAPIError):
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_thread (subject_id, agent, agent_session_id)"
                    " VALUES (CAST(:s AS uuid), 'minecraft_dave', CAST(:a AS uuid))"
                ),
                {"s": desk.subject_id, "a": sid},
            )
            await s.commit()


async def _owner_id(maker: Maker) -> str:
    from jbrain.tasks.scheduler import _owner_principal_id

    found = await _owner_principal_id(maker)
    if found is None:
        found = await service.rotate_owner_key(SqlAuthRepo(maker)) and await _owner_principal_id(
            maker
        )
    assert found
    return found


async def test_one_rolling_thread_per_agent_and_clear_starts_another(maker: Maker) -> None:
    desk = await _device(maker, "panel thread-roll", "display")
    sessions = AgentSessionRepo(maker)
    owner = SessionContext(principal_id=await _owner_id(maker), principal_kind="owner")

    def thread(agent: panel_agent.PanelAgent, fresh: bool = False) -> Any:
        return panel_agent.panel_thread(maker, sessions, owner, desk.subject_id, agent, fresh=fresh)

    jerv = await thread("jerv")
    assert await thread("jerv") == jerv, "the same thread until it is cleared"
    dave = await thread("dave")
    assert dave != jerv, "Jerv's and Dave's histories never mix"
    made = await sessions.get(owner, dave)
    assert made is not None and made.agent == "minecraft_dave" and made.title == "Desk panel"
    assert (await sessions.get(owner, jerv)).agent == "jerv"  # type: ignore[union-attr]

    cleared = await thread("jerv", fresh=True)
    assert cleared not in (jerv, dave)
    assert await thread("dave") == dave, "clearing Jerv leaves Dave's thread alone"
    assert await sessions.get(owner, jerv) is not None, "the old thread stays in the PWA"

    # A thread the owner deletes in the PWA is replaced, not resurrected.
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text("DELETE FROM app.agent_sessions WHERE id = CAST(:i AS uuid)"), {"i": cleared}
        )
        await s.commit()
    assert await thread("jerv") not in (jerv, cleared)


async def test_the_seed_switches_on_only_an_unambiguous_owner_panel(maker: Maker) -> None:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0233_panel_agent_access.py"
    spec = importlib.util.spec_from_file_location("m0233", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seed = text(module.SEED_OWNER_PANEL)

    async def run_seed() -> None:
        async with scoped_session(maker, OWNER) as s:
            await s.execute(seed)
            await s.commit()

    twin = await _device(maker, "panel Jeff", "jpet")  # the right name, the wrong role
    await run_seed()
    jeff = await _device(maker, "panel Jeff", "display")
    await run_seed()
    assert await panel_agent.agent_allowed(maker, jeff)
    assert not await panel_agent.agent_allowed(maker, twin)

    # Two live display panels both called Jeff: ambiguous, so the seed touches neither.
    from jbrain.devices.repo import SqlDeviceRepo

    await _switch(maker, jeff.subject_id, False)
    await SqlDeviceRepo(maker).provision(
        OWNER, label="panel Jeff", key_hash="x" * 64, device_role="display"
    )
    await run_seed()
    assert not await panel_agent.agent_allowed(maker, jeff)


async def test_siblings_called_directly_by_the_owner(maker: Maker) -> None:
    await _device(maker, "panel sib-a", "jpet")
    owner = PrincipalInfo(id=await _owner_id(maker), kind="owner", label="owner")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=maker)))
    got = await panel_agent.siblings(owner, request)  # type: ignore[arg-type]
    assert "sib-a" in [s.name for s in got.siblings]
    no_subject = PrincipalInfo(id="x", kind="device_key", label="x")
    with pytest.raises(HTTPException):
        await panel_agent.siblings(no_subject, request)  # type: ignore[arg-type]
    desk = await _device(maker, "panel sib-desk", "display")
    seen = await panel_agent.siblings(desk, request)  # type: ignore[arg-type]
    assert "sib-a" in [s.name for s in seen.siblings]
    assert desk.id not in [s.device_id for s in seen.siblings]
    twin = await _device(maker, "panel sib-twin", "jpet")
    with pytest.raises(HTTPException) as caught:
        await panel_agent.siblings(twin, request)  # type: ignore[arg-type]
    assert caught.value.status_code == 403


async def test_the_switch_called_directly(maker: Maker) -> None:
    desk = await _device(maker, "panel switch-direct", "display")
    twin = await _device(maker, "panel switch-twin", "jpet")
    on = await panel_agent.write_access(maker, OWNER, desk.id, True)
    assert on.agent_access
    assert (await panel_agent.read_access(maker, OWNER, desk.id)).agent_access
    with pytest.raises(HTTPException) as caught:
        await panel_agent.write_access(maker, OWNER, twin.id, True)
    assert caught.value.status_code == 409


def test_the_owner_s_own_routes(box: Box) -> None:
    """The PWA's half, under the owner's cookie (the fixture's panel calls clear it, so the
    owner dependency is supplied directly)."""
    from jbrain.api import deps

    owner = PrincipalInfo(id=OWNER.principal_id or "", kind="owner", label="owner")
    app = box.client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[deps.owner_only] = lambda: owner
    try:
        url = f"/api/endpoint/panels/{box.devices['Jeff']}/agent"
        assert box.client.put(url, json={"agent_access": True}).json() == {"agent_access": True}
        assert box.client.get(url).json() == {"agent_access": True}
    finally:
        app.dependency_overrides.clear()
