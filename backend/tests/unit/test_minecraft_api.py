"""The owner's Minecraft routes (MINECRAFT_BEDROCK_PLAN §M1) against a faked sidecar.

Pinned here is what the screen relies on: the server view flattens the wrapper's
properties into the contract's fields, a stopped container reads as a state, the
release notes describe the version on offer when an update is waiting, restart is a
stop-then-start, and "who's online" comes from the live server.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.api import minecraft as api
from jbrain.minecraft import changelog, sessions
from jbrain.minecraft import client as mc

SETTINGS: Any = SimpleNamespace(minecraft_url="http://x", minecraft_token="t")
STATUS = {
    "state": "running",
    "install_error": None,
    "version": "1.26.52.3",
    "level_name": "world",
    "properties": {
        "server-name": "JBrain",
        "gamemode": "survival",
        "difficulty": "normal",
        "allow-list": "false",
        "server-port": "19132",
    },
    "lan_ip": "192.168.1.20",
    "uptime_s": 120,
    "players": [{"name": "Steve42", "xuid": "111", "joined_at": 100.0}],
    "update": {"state": "idle"},
    "auto_update": False,
}


class FakeChangelog:
    def __init__(self) -> None:
        self.asked: list[str | None] = []

    async def notes_for(self, version: str | None) -> changelog.Notes | None:
        self.asked.append(version)
        if version == "1.26.60.4":
            return changelog.Notes(version, "26.60 Changelog", "u", "d", ["line"])
        return None


def _request(cl: FakeChangelog | None = None) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(minecraft_changelog=cl or FakeChangelog(), session_maker=None)
        )
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "container": {"service": "minecraft", "state": "running"},
        "replies": {"/status": STATUS},
        "calls": [],
        "lifecycle": [],
    }

    async def call(_s: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        state["calls"].append((method, path, kw))
        reply = state["replies"].get(path)
        if isinstance(reply, Exception):
            raise reply
        return reply or {}

    async def container(_r: Any, _s: Any) -> Any:
        return state["container"]

    async def lifecycle(_r: Any, _s: Any, action: str) -> None:
        state["lifecycle"].append(action)

    monkeypatch.setattr(mc, "call", call)
    monkeypatch.setattr(mc, "container", container)
    monkeypatch.setattr(mc, "lifecycle", lifecycle)
    return state


async def test_the_server_view_matches_the_contract(fake) -> None:
    out = await api.minecraft(_request(), SETTINGS)
    s = out["server"]
    assert (s["server_name"], s["gamemode"], s["allow_list"], s["port"]) == (
        "JBrain",
        "survival",
        False,
        19132,
    )
    assert s["lan_ip"] == "192.168.1.20" and s["players"][0]["joined_at"] == 100.0


async def test_a_stopped_container_is_a_state_not_an_error(fake) -> None:
    fake["container"] = {"service": "minecraft", "state": "exited"}
    out = await api.minecraft(_request(), SETTINGS)
    assert out == {"container": fake["container"], "server": None, "server_error": None}


async def test_an_unreachable_wrapper_is_named(fake) -> None:
    fake["replies"]["/status"] = HTTPException(status_code=503, detail="unreachable")
    out = await api.minecraft(_request(), SETTINGS)
    assert out["server"] is None and out["server_error"] == "unreachable"


async def test_notes_describe_the_update_on_offer(fake) -> None:
    fake["replies"]["/version"] = {
        "running": "1.26.52.3",
        "latest": "1.26.60.4",
        "update_available": True,
    }
    cl = FakeChangelog()
    out = await api.version(_request(cl), SETTINGS, refresh=True)
    assert cl.asked == ["1.26.60.4"]
    assert out["notes"]["title"] == "26.60 Changelog"
    assert out["notes_fallback_url"] == changelog.FALLBACK_URL
    assert fake["calls"][0][2]["params"] == {"refresh": 1}


async def test_unpublished_notes_are_null_with_a_fallback(fake) -> None:
    fake["replies"]["/version"] = {"running": "1.26.52.3", "latest": "1.26.52.3"}
    out = await api.version(_request(), SETTINGS)
    assert out["notes"] is None and out["notes_fallback_url"]


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_lifecycle_acts_on_the_game_server_and_keeps_the_container(fake, action) -> None:
    # Stopping BDS rather than the container keeps status, facts and updates reachable.
    await getattr(api, action)(_request(), SETTINGS)
    assert fake["lifecycle"] == []
    assert fake["calls"][-1][:2] == ("POST", f"/server/{action}")


async def test_start_with_the_container_down_brings_it_up_first(fake) -> None:
    fake["container"] = {"service": "minecraft", "state": "exited"}
    await api.restart(_request(), SETTINGS)
    assert fake["lifecycle"] == ["start"]
    assert fake["calls"][-1][:2] == ("POST", "/server/start")


async def test_stopping_a_stopped_container_does_nothing(fake) -> None:
    fake["container"] = None
    assert await api.stop(_request(), SETTINGS) == {"action": "stop"}
    assert fake["lifecycle"] == [] and fake["calls"] == []


async def test_settings_pass_auto_update_through(fake) -> None:
    await api.put_settings(api.SettingsIn(auto_update=True), SETTINGS)
    assert fake["calls"][-1][:2] == ("POST", "/settings")
    assert fake["calls"][-1][2]["json"] == {"auto_update": True}


async def test_players_take_who_is_online_from_the_live_server(
    fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def players(_maker: Any, online: dict[str, float]) -> list[dict[str, Any]]:
        seen["online"] = online
        return []

    monkeypatch.setattr(sessions, "players", players)
    out = await api.players(_request(), SETTINGS)
    assert seen["online"] == {"111": 100.0}
    assert out == {"players": [], "stats_available": False}


async def test_players_still_answer_when_the_server_is_stopped(
    fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake["replies"]["/status"] = HTTPException(status_code=503, detail="stopped")

    async def players(_maker: Any, online: dict[str, float]) -> list[dict[str, Any]]:
        assert online == {}
        return [{"xuid": "111"}]

    monkeypatch.setattr(sessions, "players", players)
    assert (await api.players(_request(), SETTINGS))["players"] == [{"xuid": "111"}]


def test_every_route_is_owner_only() -> None:
    # The gate is on the router, so a route added later inherits it.
    from jbrain.api.deps import owner_only

    assert [d.dependency for d in api.router.dependencies] == [owner_only]


async def test_retry_install_restarts_the_container(fake) -> None:
    await api.retry_install(_request(), SETTINGS)
    assert fake["lifecycle"] == ["stop", "start"]


async def test_a_down_supervisor_is_named_not_a_500(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(_r: Any, _s: Any) -> Any:
        raise HTTPException(status_code=503, detail="supervisor unreachable (ConnectError)")

    monkeypatch.setattr(mc, "container", down)
    out = await api.minecraft(_request(), SETTINGS)
    assert out == {
        "container": None,
        "server": None,
        "server_error": "supervisor unreachable (ConnectError)",
    }
