"""The desk panel's conversation switcher (firmware-c6 0.0.15), against real Postgres and the app.

What must hold: only a panel the talk would let through reaches Recent, switch or a thread's
messages (the twins' panels and an un-switched display never); what they see and pick is the
live owner's root chats with THAT agent, started at the panel or in the PWA, and nothing else
— whatever id the panel sends; and what the panel is given fits what it keeps.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jbrain.agent.session import AgentSessionRepo
from jbrain.api import panel_agent, panel_convo
from jbrain.auth.owner import owner_principal_id
from jbrain.db.session import SessionContext, panel_context, scoped_session
from tests.conftest import docker_available
from tests.integration import test_panel_agent_pg as agent_pg
from tests.integration.test_panel_agent_pg import Box, Maker, _owner_put
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

# The desk panel and its box, shared with the talk's own suite.
box = agent_pg.box
maker = agent_pg.maker

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

T0 = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
LONG = " ".join(f"Sentence number {i} says a little more about the tides." for i in range(60))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _owner_ctx(maker: Maker) -> SessionContext:
    """The live owner the panel acts for; a box with none yet (a test run alone) gets one."""
    pid = await owner_principal_id(maker) or await agent_pg._owner_id(maker)
    return SessionContext(principal_id=pid, principal_kind="owner")


async def _chat(
    maker: Maker,
    ctx: SessionContext,
    *,
    agent: str = "jerv",
    title: str = "",
    origin: str | None = "pwa",
    turns: tuple[tuple[str, str], ...] = (("user", "hi"), ("assistant", "Hello.")),
    minutes_ago: int = 0,
    parent: str | None = None,
) -> str:
    made = await AgentSessionRepo(maker).create(
        ctx,
        domain_scopes=[],
        title=title,
        agent=agent,
        origin=origin,
        parent_session_id=parent,
        depth=1 if parent else 0,
    )
    async with scoped_session(maker, ctx) as s:
        for role, content in turns:
            await s.execute(
                text(
                    "INSERT INTO app.agent_turns (id, session_id, role, content)"
                    " VALUES (gen_random_uuid(), CAST(:s AS uuid), :r, :c)"
                ),
                {"s": made.id, "r": role, "c": content},
            )
        await s.execute(
            text("UPDATE app.agent_sessions SET last_active_at = :t WHERE id = CAST(:s AS uuid)"),
            {"t": T0 - timedelta(minutes=minutes_ago), "s": made.id},
        )
        await s.commit()
    return made.id


async def _subject(maker: Maker, device_id: str) -> str:
    async with scoped_session(maker, OWNER) as s:
        return str(
            (
                await s.execute(
                    text("SELECT subject_id::text FROM app.principals WHERE id = CAST(:d AS uuid)"),
                    {"d": device_id},
                )
            ).scalar_one()
        )


async def _point(maker: Maker, subject: str, agent: str, sid: str) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.endpoint_panel_thread (subject_id, agent, agent_session_id)"
                " VALUES (CAST(:s AS uuid), :a, CAST(:i AS uuid)) ON CONFLICT (subject_id, agent)"
                " DO UPDATE SET agent_session_id = EXCLUDED.agent_session_id"
            ),
            {"s": subject, "a": agent, "i": sid},
        )
        await s.commit()


async def _stranger(maker: Maker) -> SessionContext:
    """A principal that is not the owner the panel acts for: the debug capability's."""
    async with scoped_session(maker, OWNER) as s:
        pid = (
            await s.execute(
                text(
                    "SELECT id::text FROM app.principals WHERE kind = 'capability_token'"
                    " ORDER BY created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
    return SessionContext(principal_id=str(pid), principal_kind="owner")


def _recent(box: Box, who: str = "Jeff", agent: str = "jerv") -> Any:
    return box.panel(who, "GET", f"/api/endpoint/convo/{agent}/recent")


# ---- the gate ---------------------------------------------------------------------------------


def test_a_twin_s_panel_and_an_unswitched_display_reach_none_of_it(box: Box) -> None:
    calls = (
        ("GET", "/api/endpoint/convo/jerv/recent", None),
        ("POST", "/api/endpoint/convo/jerv/select", {"session_id": str(uuid.uuid4())}),
        ("GET", "/api/endpoint/convo/dave/messages", None),
    )
    for method, path, body in calls:
        assert box.panel("Jeff", method, path, json=body).status_code == 403, path
    # A twin's panel, even with the flag forced on its row behind the API's back.
    box.sql(
        "INSERT INTO app.endpoint_panel (subject_id, agent_access)"
        " SELECT subject_id, true FROM app.principals WHERE id = CAST(:d AS uuid)",
        d=box.devices["Quorra"],
    )
    _owner_put(box, "Jeff", True)
    for method, path, body in calls:
        assert box.panel("Quorra", method, path, json=body).status_code == 403, path
        assert box.panel("Tamsin", method, path, json=body).status_code == 403, path
    # No key at all.
    box.client.cookies.clear()
    assert box.client.get("/api/endpoint/convo/jerv/recent").status_code == 401
    assert _recent(box, agent="admin").status_code == 422


# ---- Recent --------------------------------------------------------------------------------------


def test_recent_is_the_owner_s_panel_and_pwa_chats_with_that_agent(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    m = box.maker
    owner = _run(_owner_ctx(m))
    subject = _run(_subject(m, box.devices["Jeff"]))
    panel = _run(
        _chat(
            m,
            owner,
            title="Desk panel",
            origin="panel",
            turns=(("user", "Add milk to the list."), ("assistant", "Added milk to your list.")),
            minutes_ago=50,
        )
    )
    tides = _run(
        _chat(
            m,
            owner,
            title="Tides at Port St John",
            turns=(("assistant", "Low at 14:52."), ("user", "Good for **rock pools** later?")),
            minutes_ago=12,
        )
    )
    untitled = _run(_chat(m, owner, title="", minutes_ago=30))
    # Never listed: another agent, the game door's (no origin), a sub-agent child, a chat with
    # nothing said, an archived one, and another principal's.
    _run(_chat(m, owner, agent="minecraft_dave", title="Wheat farm", minutes_ago=1))
    _run(_chat(m, owner, title="In game: Steve", origin=None, minutes_ago=1))
    _run(_chat(m, owner, title="child", parent=tides, minutes_ago=1))
    _run(_chat(m, owner, title="Blank", turns=(), minutes_ago=1))
    archived = _run(_chat(m, owner, title="Archived", minutes_ago=1))
    box.sql(
        "UPDATE app.agent_sessions SET status = 'archived' WHERE id = CAST(:i AS uuid)", i=archived
    )
    _run(_chat(m, _run(_stranger(m)), title="Not his", minutes_ago=1))
    _run(_point(m, subject, "jerv", panel))

    r = _recent(box)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["id"] for i in items] == [panel, tides, untitled], "current first, then newest"
    first, second, third = items
    assert first == {
        "id": panel,
        "title": "Desk panel",
        "at": int((T0 - timedelta(minutes=50)).timestamp()),
        "origin": "panel",
        "snippet": "Added milk to your list.",
        "current": True,
    }
    assert second["origin"] == "pwa" and not second["current"]
    assert second["snippet"] == "You: Good for rock pools later?", "the owner's line, plain"
    assert third["title"] == "Untitled chat"

    # Dave's list is Dave's.
    dave = _recent(box, agent="dave").json()["items"]
    assert [i["title"] for i in dave] == ["Wheat farm"]


def test_recent_keeps_ten_rows_each_cut_to_what_a_row_holds(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    m = box.maker
    owner = _run(_owner_ctx(m))
    for n in range(12):
        _run(
            _chat(
                m,
                owner,
                agent="minecraft_dave",
                title=f"A very long title about the wheat farm and the river, number {n}",
                turns=(("assistant", LONG),),
                minutes_ago=n,
            )
        )
    items = _recent(box, agent="dave").json()["items"]
    assert len(items) == panel_convo.RECENT_MAX
    for i in items:
        assert len(i["title"].encode()) <= panel_convo.TITLE_BYTES and i["title"].endswith("…")
        assert len(i["snippet"].encode()) <= panel_convo.SNIPPET_BYTES
        assert i["snippet"].endswith("…")
        assert set(i) == {"id", "title", "at", "origin", "snippet", "current"}
    assert not any(i["current"] for i in items), "this panel has no Dave thread yet"


# ---- switch -------------------------------------------------------------------------------------


def test_select_points_the_next_talk_at_the_chosen_chat(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    m = box.maker
    owner = _run(_owner_ctx(m))
    subject = _run(_subject(m, box.devices["Jeff"]))
    sessions = AgentSessionRepo(m)
    before = _run(panel_agent.panel_thread(m, sessions, owner, subject, "jerv", fresh=False))
    tides = _run(_chat(m, owner, title="Tides at Port St John", minutes_ago=5))

    r = box.panel("Jeff", "POST", "/api/endpoint/convo/jerv/select", json={"session_id": tides})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": tides, "title": "Tides at Port St John"}
    now = _run(panel_agent.panel_thread(m, sessions, owner, subject, "jerv", fresh=False))
    assert now == tides != before, "the next talk continues the chosen chat"
    current = [i for i in _recent(box).json()["items"] if i["current"]]
    assert [i["id"] for i in current] == [tides]
    # Dave's thread is not touched by a Jerv switch.
    dave = _run(panel_agent.panel_thread(m, sessions, owner, subject, "dave", fresh=False))
    assert dave not in (before, tides)


def test_select_refuses_what_the_list_would_not_show(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    m = box.maker
    owner = _run(_owner_ctx(m))
    subject = _run(_subject(m, box.devices["Jeff"]))
    sessions = AgentSessionRepo(m)
    kept = _run(panel_agent.panel_thread(m, sessions, owner, subject, "jerv", fresh=False))
    root = _run(_chat(m, owner, title="root"))
    refused = {
        "another agent's": _run(_chat(m, owner, agent="minecraft_dave", title="Wheat farm")),
        "a child": _run(_chat(m, owner, title="child", parent=root)),
        "the game door's": _run(_chat(m, owner, title="In game: Steve", origin=None)),
        "another principal's": _run(_chat(m, _run(_stranger(m)), title="Not his")),
        "a missing one": str(uuid.uuid4()),
        "not a uuid": "'; DROP TABLE app.agent_sessions; --",
    }
    for why, sid in refused.items():
        r = box.panel("Jeff", "POST", "/api/endpoint/convo/jerv/select", json={"session_id": sid})
        assert r.status_code == 404, why
    assert _run(panel_agent.panel_thread(m, sessions, owner, subject, "jerv", fresh=False)) == kept
    bad = box.panel("Jeff", "POST", "/api/endpoint/convo/jerv/select", json={})
    assert bad.status_code == 422


# ---- a switched thread's messages -------------------------------------------------------------


def test_messages_are_the_last_exchanges_in_the_panel_s_bounds(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    m = box.maker
    owner = _run(_owner_ctx(m))
    subject = _run(_subject(m, box.devices["Jeff"]))
    empty = box.panel("Jeff", "GET", "/api/endpoint/convo/jerv/messages").json()
    assert empty == {"id": "", "title": "", "messages": [], "more": False}, "no thread yet"

    turns = (
        ("user", "First question?"),
        ("assistant", "First answer."),
        ("user", panel_agent.LEGACY_VOICE_PREFIX + "How big was a T-Rex? " + LONG),
        ("assistant", LONG),
        ("user", "And the best one found?"),
        ("assistant", "## Sue\n\n- In **Chicago**. " + LONG),
    )
    sid = _run(_chat(m, owner, title="T-Rex fossils", turns=turns))
    _run(_point(m, subject, "jerv", sid))

    got = box.panel("Jeff", "GET", "/api/endpoint/convo/jerv/messages?limit=2").json()
    assert got["id"] == sid and got["title"] == "T-Rex fossils"
    assert got["more"] is True, "older turns stay in the app"
    msgs = got["messages"]
    assert [x["mine"] for x in msgs] == [True, False, True, False]
    assert msgs[0]["text"].startswith("How big was a T-Rex?"), "the legacy prefix is not his"
    for x in msgs[:-1]:
        assert len(x["text"].encode()) <= panel_convo.BUBBLE_BYTES
    newest = msgs[-1]["text"]
    assert newest.startswith("Sue. In Chicago."), "plain prose, no Markdown"
    assert len(newest.encode()) <= panel_convo.NEWEST_BYTES < len(LONG.encode())
    assert newest.endswith(panel_agent.MORE.strip()), "an answer cut says the rest is in the app"

    whole = box.panel("Jeff", "GET", "/api/endpoint/convo/jerv/messages?limit=4").json()
    assert whole["more"] is False and len(whole["messages"]) == 6
    title = box.panel("Jeff", "GET", "/api/endpoint/convo/jerv/messages?limit=0").json()
    assert title == {"id": sid, "title": "T-Rex fossils", "messages": [], "more": True}
    too_many = box.panel("Jeff", "GET", "/api/endpoint/convo/jerv/messages?limit=9")
    assert too_many.status_code == 422


def test_a_thread_left_on_another_principal_reads_as_none(box: Box) -> None:
    """The panel's row can only name a chat; it is the owner's chat or it is nothing."""
    _owner_put(box, "Jeff", True)
    m = box.maker
    subject = _run(_subject(m, box.devices["Jeff"]))
    theirs = _run(_chat(m, _run(_stranger(m)), title="Not his"))
    _run(_point(m, subject, "jerv", theirs))
    got = box.panel("Jeff", "GET", "/api/endpoint/convo/jerv/messages").json()
    assert got == {"id": "", "title": "", "messages": [], "more": False}
    assert all(i["id"] != theirs for i in _recent(box).json()["items"])


# ---- the column ---------------------------------------------------------------------------------


async def test_a_panel_reads_and_writes_no_session_s_origin(maker: Maker) -> None:
    """`agent_sessions.origin` is under the owner-only policy like the rest of the row."""
    owner = await _owner_ctx(maker)
    sid = await _chat(maker, owner, title="mine", origin="pwa")
    device = str(uuid.uuid4())
    async with scoped_session(maker, panel_context(device, str(uuid.uuid4()))) as s:
        seen = (
            await s.execute(
                text("SELECT count(*) FROM app.agent_sessions WHERE origin IS NOT NULL")
            )
        ).scalar_one()
        assert seen == 0
        moved = await s.execute(
            text("UPDATE app.agent_sessions SET origin = 'panel' WHERE id = CAST(:i AS uuid)"),
            {"i": sid},
        )
        assert moved.rowcount == 0  # type: ignore[attr-defined]
        await s.commit()
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, owner) as s:
            await s.execute(
                text("UPDATE app.agent_sessions SET origin = 'game' WHERE id = CAST(:i AS uuid)"),
                {"i": sid},
            )


async def test_the_backfill_marks_only_what_the_rows_can_tell(maker: Maker) -> None:
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0238_agent_session_origin.py"
    spec = importlib.util.spec_from_file_location("m0238", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    owner = await _owner_ctx(maker)
    made = {
        "desk": await _chat(maker, owner, title="Desk panel", origin=None),
        "pwa": await _chat(maker, owner, title="Pancakes", origin=None),
        "dave_pwa": await _chat(maker, owner, agent="minecraft_dave", title="Farm", origin=None),
        "curator": await _chat(maker, owner, agent="curator", title="Notes", origin=None),
    }
    made["child"] = await _chat(maker, owner, title="child", origin=None, parent=made["pwa"])
    made["door"] = await _chat(maker, owner, agent="minecraft_dave", title="In game", origin=None)
    async with scoped_session(maker, owner) as s:
        await s.execute(
            text(
                "INSERT INTO app.mc_chat_player (agent_session_id, xuid, gamertag, door)"
                " VALUES (CAST(:s AS uuid), :k, 'Steve', true)"
            ),
            {"s": made["door"], "k": uuid.uuid4().hex},
        )
        await s.execute(text(module.BACKFILL_PANEL))
        await s.execute(text(module.BACKFILL_PWA))
        await s.commit()
    async with scoped_session(maker, owner) as s:
        found = (
            await s.execute(
                text(
                    "SELECT id::text, origin FROM app.agent_sessions"
                    " WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": list(made.values())},
            )
        ).all()
    rows: dict[str, str | None] = {str(r[0]): r[1] for r in found}
    got = {k: rows[v] for k, v in made.items()}
    assert got == {
        "desk": "panel",
        "pwa": "pwa",
        "dave_pwa": "pwa",
        "curator": None,
        "child": None,
        "door": None,
    }


async def test_the_routes_called_directly(maker: Maker) -> None:
    """The three handlers on the test's own loop, past the HTTP layer: the gate first, then the
    owner's chats only."""
    desk = await agent_pg._device(maker, "panel convo-direct", "display")
    twin = await agent_pg._device(maker, "panel convo-twin", "jpet")
    await agent_pg._switch(maker, desk.subject_id, True)
    await agent_pg._switch(maker, twin.subject_id, True)
    owner = await _owner_ctx(maker)
    request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=maker)))
    for call in (
        panel_convo.recent("jerv", twin, request),
        panel_convo.select("jerv", panel_convo.Select(session_id=str(uuid.uuid4())), twin, request),
        panel_convo.messages("jerv", twin, request, limit=1),
    ):
        with pytest.raises(HTTPException) as refused:
            await call
        assert refused.value.status_code == 403

    sid = await _chat(
        maker,
        owner,
        title="",
        origin="panel",
        turns=(("user", "Add milk."), ("assistant", "Added milk.")),
        minutes_ago=-60,
    )
    listed = await panel_convo.recent("jerv", desk, request)
    assert [(i.id, i.title, i.current) for i in listed.items][:1] == [(sid, "Desk panel", False)]
    picked = await panel_convo.select("jerv", panel_convo.Select(session_id=sid), desk, request)
    assert picked.id == sid and picked.title == "Desk panel"
    with pytest.raises(HTTPException) as missing:
        await panel_convo.select("dave", panel_convo.Select(session_id=sid), desk, request)
    assert missing.value.status_code == 404
    thread = await panel_convo.messages("jerv", desk, request, limit=1)
    assert thread.id == sid and [m.text for m in thread.messages] == ["Add milk.", "Added milk."]
    assert not thread.more
    none = await panel_convo.messages("dave", desk, request, limit=1)
    assert none.id == "" and none.messages == []
