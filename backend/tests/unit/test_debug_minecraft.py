"""The debug console's handle on the Minecraft sidecar (MINECRAFT_BEDROCK_PLAN §3a).

The routes are thin proxies; what is pinned here is the behaviour an assistant relies
on with no terminal behind it: a stopped server reads as a state rather than a crash,
the sidecar's refusals arrive as refusals, and a restart is a stop-then-start that
honours the grace period rather than docker's 10-second restart.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import HTTPException

from jbrain.api import debug_minecraft as mc
from jbrain.minecraft import client as mc_client

SETTINGS: Any = SimpleNamespace(
    minecraft_url="http://host.docker.internal:19180",
    minecraft_token="mc-tok",
    supervisor_token="t",
)
PRINCIPAL: Any = SimpleNamespace(id="x", label="l", kind="capability_token")


class FakeSupervisor:
    def __init__(self, containers: list[dict[str, Any]], *, missing: bool = False) -> None:
        self.containers = containers
        self.missing = missing
        self.calls: list[tuple[str, Any]] = []

    async def get(self, path: str, **_kw: Any) -> httpx.Response:
        self.calls.append(("GET", path))
        req = httpx.Request("GET", f"http://supervisor{path}")
        return httpx.Response(200, json={"containers": self.containers}, request=req)

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        return await (self.get(path, **kw) if method == "GET" else self.post(path, **kw))

    async def post(self, path: str, **kw: Any) -> httpx.Response:
        self.calls.append(("POST", path))
        req = httpx.Request("POST", f"http://supervisor{path}")
        if self.missing:
            return httpx.Response(404, json={"detail": "unknown"}, request=req)
        return httpx.Response(202, json={"service": kw["json"]["service"]}, request=req)


def _request(sup: FakeSupervisor) -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(supervisor_client=sup)),
        state=SimpleNamespace(),
    )


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch):
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies.get(request.url.path, httpx.Response(200, json={"ok": True}))

    monkeypatch.setattr(mc_client, "_transport", httpx.MockTransport(handler))
    return seen, replies


RUNNING = [{"service": "minecraft", "state": "running", "health": "healthy"}]


async def test_status_returns_the_container_and_the_server_side_by_side(sidecar) -> None:
    _, replies = sidecar
    replies["/status"] = httpx.Response(200, json={"state": "running", "version": "1.26"})
    out = await mc.minecraft_status(_request(FakeSupervisor(RUNNING)), SETTINGS, PRINCIPAL)
    assert out["container"]["state"] == "running"
    assert out["server"] == {"state": "running", "version": "1.26"}
    assert out["server_error"] is None


async def test_a_stopped_container_is_not_asked_for_server_status(sidecar) -> None:
    seen, _ = sidecar
    stopped = [{"service": "minecraft", "state": "exited"}]
    out = await mc.minecraft_status(_request(FakeSupervisor(stopped)), SETTINGS, PRINCIPAL)
    assert out["container"]["state"] == "exited"
    assert out["server"] is None
    assert seen == []


async def test_a_container_that_was_never_created_reads_as_none(sidecar) -> None:
    out = await mc.minecraft_status(_request(FakeSupervisor([])), SETTINGS, PRINCIPAL)
    assert out == {"container": None, "server": None, "server_error": None}


async def test_an_unreachable_sidecar_is_named_in_the_status_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(mc_client, "_transport", httpx.MockTransport(refuse))
    out = await mc.minecraft_status(_request(FakeSupervisor(RUNNING)), SETTINGS, PRINCIPAL)
    assert out["server"] is None
    assert "unreachable" in (out["server_error"] or "")


async def test_restart_is_a_stop_then_a_start_never_a_docker_restart() -> None:
    sup = FakeSupervisor(RUNNING)
    await mc.minecraft_restart(_request(sup), SETTINGS, PRINCIPAL)
    assert sup.calls == [("POST", "/stop"), ("POST", "/start")]


async def test_lifecycle_on_a_missing_container_says_to_deploy() -> None:
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_start(_request(FakeSupervisor([], missing=True)), SETTINGS, PRINCIPAL)
    assert exc.value.status_code == 404
    assert "/debug/update" in str(exc.value.detail)


async def test_console_forwards_the_command_and_its_wait(sidecar) -> None:
    seen, replies = sidecar
    replies["/command"] = httpx.Response(200, json={"command": "list", "lines": ["x"]})
    body = mc.ConsoleIn(command="list", wait_s=5)
    out = await mc.minecraft_console(body, _request(FakeSupervisor(RUNNING)), SETTINGS, PRINCIPAL)
    assert out["lines"] == ["x"]
    assert json.loads(seen[0].content) == {"command": "list", "wait_s": 5.0}


async def test_a_sidecar_refusal_arrives_as_a_refusal(sidecar) -> None:
    _, replies = sidecar
    replies["/command"] = httpx.Response(400, json={"detail": "`stop` is refused"})
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_console(
            mc.ConsoleIn(command="stop"),
            _request(FakeSupervisor(RUNNING)),
            SETTINGS,
            PRINCIPAL,
        )
    assert exc.value.status_code == 400
    assert "refused" in str(exc.value.detail)


async def test_properties_put_passes_nulls_through_as_removals(sidecar) -> None:
    seen, _ = sidecar
    await mc.minecraft_set_properties(
        mc.PropertiesIn(set={"transport": "nethernet", "allow-list": None}),
        _request(FakeSupervisor(RUNNING)),
        SETTINGS,
        PRINCIPAL,
    )
    assert json.loads(seen[0].content) == {"set": {"transport": "nethernet", "allow-list": None}}


async def test_no_minecraft_url_is_a_503_naming_the_absence() -> None:
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_snapshots(
            _request(FakeSupervisor(RUNNING)),
            cast(Any, SimpleNamespace(minecraft_url="", minecraft_token="", supervisor_token="t")),
            PRINCIPAL,
        )
    assert exc.value.status_code == 503


async def test_every_sidecar_call_carries_the_bearer(sidecar) -> None:
    seen, _ = sidecar
    await mc.minecraft_snapshots(_request(FakeSupervisor(RUNNING)), SETTINGS, PRINCIPAL)
    assert seen[0].headers["Authorization"] == "Bearer mc-tok"


async def test_a_sidecar_auth_refusal_is_a_deploy_fault_not_a_state(sidecar) -> None:
    _, replies = sidecar
    replies["/snapshots"] = httpx.Response(401, json={"detail": "unauthorized"})
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_snapshots(_request(FakeSupervisor(RUNNING)), SETTINGS, PRINCIPAL)
    assert exc.value.status_code == 502


async def test_a_missing_token_is_named_instead_of_a_malformed_header() -> None:
    settings: Any = SimpleNamespace(
        minecraft_url="http://host.docker.internal:19180",
        minecraft_token="",
        supervisor_token="t",
    )
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_snapshots(_request(FakeSupervisor(RUNNING)), settings, PRINCIPAL)
    assert exc.value.status_code == 503
    assert "MINECRAFT_TOKEN" in str(exc.value.detail)


async def test_update_and_probe_pack_reach_their_sidecar_routes(sidecar) -> None:
    seen, replies = sidecar
    replies["/update"] = httpx.Response(202, json={"state": "backing_up"})
    req = _request(FakeSupervisor(RUNNING))
    assert (await mc.minecraft_update(req, SETTINGS, PRINCIPAL))["state"] == "backing_up"
    await mc.minecraft_probe_pack(mc.ProbePackIn(install=False), req, SETTINGS, PRINCIPAL)
    assert [r.url.path for r in seen] == ["/update", "/probe-pack"]
    assert json.loads(seen[1].content) == {"install": False}


async def test_a_supervisor_transport_error_becomes_a_503() -> None:
    class Down:
        async def request(self, *_a: Any, **_k: Any) -> httpx.Response:
            raise httpx.ConnectError("refused")

    req: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor_client=Down())))
    with pytest.raises(HTTPException) as exc:
        await mc_client.container(req, SETTINGS)
    assert exc.value.status_code == 503 and "supervisor unreachable" in str(exc.value.detail)


async def test_worlds_and_rules_are_read_only_views_of_the_sidecar(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds"] = httpx.Response(200, json={"slots": [{"id": "slot1"}]})
    req = _request(FakeSupervisor([]))
    assert (await mc.minecraft_worlds(req, SETTINGS, PRINCIPAL))["slots"][0]["id"] == "slot1"
    await mc.minecraft_world_rules("slot2", req, SETTINGS, PRINCIPAL)
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/worlds"),
        ("GET", "/worlds/slot2/rules"),
    ]
    # The owner's worlds are read-only here: the only world-changing routes are a load,
    # the test worlds' own make and reset, each guarded below (§SW4), and the Power Pack
    # switch, a game rule the owner has no other no-terminal way to flip yet (§M10).
    paths = {
        (m, getattr(r, "path", "")) for r in mc.router.routes for m in getattr(r, "methods", ())
    }
    assert {(m, p) for m, p in paths if "worlds" in p and m != "GET"} == {
        ("POST", "/debug/minecraft/worlds/{slot}/load"),
        ("POST", "/debug/minecraft/test-worlds/{slot}"),
        ("POST", "/debug/minecraft/test-worlds/{slot}/reset"),
        ("PUT", "/debug/minecraft/worlds/{slot}/power-pack"),
    }


async def test_the_power_pack_switch_passes_through_and_is_recorded(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds/slot2/power-pack"] = httpx.Response(
        200, json={"id": "slot2", "power_pack_keep": True, "applies": "now"}
    )
    req = _request(FakeSupervisor([]))
    out = await mc.minecraft_power_pack("slot2", mc.PowerPackIn(on=True), req, SETTINGS, PRINCIPAL)
    assert out["power_pack_keep"] is True
    assert seen[-1].method == "POST" and json.loads(seen[-1].content) == {"on": True}
    assert req.state.debug_detail == "minecraft power pack slot2 on=True"
    replies["/worlds/slot2/power-pack"] = httpx.Response(400, json={"detail": "bad"})
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_power_pack("slot2", mc.PowerPackIn(on=False), req, SETTINGS, PRINCIPAL)
    assert exc.value.status_code == 400


async def test_travel_reports_the_log_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def summary(_maker: Any) -> list[dict[str, Any]]:
        return [{"world": "world", "samples": 3}]

    monkeypatch.setattr(mc.travel, "summary", summary)
    req: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=object())),
        state=SimpleNamespace(),
    )
    out = await mc.minecraft_travel(req, PRINCIPAL)
    assert out == {"players": [{"world": "world", "samples": 3}]}
    assert req.state.debug_detail == "minecraft travel summary"


async def test_the_debug_map_serves_a_picture_not_the_world(sidecar) -> None:
    seen, replies = sidecar
    replies["/map/tile/the_end/0/1/1.png"] = httpx.Response(200, content=b"\x89PNG")
    req = _request(FakeSupervisor([]))
    resp = await mc.minecraft_map_tile("the_end", 0, 1, 1, req, SETTINGS, PRINCIPAL)
    assert resp.body == b"\x89PNG" and resp.media_type == "image/png"
    await mc.minecraft_map_info(req, SETTINGS, PRINCIPAL, slot="slot1", dim="overworld")
    assert [r.url.path for r in seen] == ["/map/tile/the_end/0/1/1.png", "/map/info"]


async def test_the_debug_point_and_nearby_pass_their_coordinates_through(sidecar) -> None:
    seen, replies = sidecar
    replies["/map/point"] = httpx.Response(200, json={"source": "survey", "biome": "Plains"})
    replies["/map/nearby"] = httpx.Response(200, json={"nearby": []})
    req = _request(FakeSupervisor([]))
    got = await mc.minecraft_map_point(req, SETTINGS, PRINCIPAL, x=5, z=-6, slot="slot2")
    assert got["biome"] == "Plains"
    await mc.minecraft_map_nearby(req, SETTINGS, PRINCIPAL, x=1, z=2, dim="nether")
    assert dict(seen[0].url.params) == {"slot": "slot2", "dim": "overworld", "x": "5", "z": "-6"}
    assert dict(seen[1].url.params) == {"dim": "nether", "x": "1", "z": "2"}


async def test_the_debug_block_finder_passes_its_search_through(sidecar) -> None:
    seen, replies = sidecar
    replies["/map/find"] = httpx.Response(200, json={"found": []})
    req = _request(FakeSupervisor([]))
    await mc.minecraft_map_find(
        req, SETTINGS, PRINCIPAL, x=1, z=2, blocks="diamond_ore", dim="nether", limit=3
    )
    assert dict(seen[0].url.params) == {
        "slot": "slot1",
        "dim": "nether",
        "x": "1",
        "z": "2",
        "blocks": "diamond_ore",
        "limit": "3",
        "radius": "2048",
    }


async def test_the_debug_structure_index_and_sweep_pass_through(sidecar) -> None:
    seen, replies = sidecar
    replies["/map/structures"] = httpx.Response(200, json={"structures": [], "stale": None})
    replies["/map/structures/sweep"] = httpx.Response(202, json={"state": "running"})
    req = _request(FakeSupervisor([]))
    got = await mc.minecraft_map_structures(req, SETTINGS, PRINCIPAL, slot="slot2", dim="the_end")
    assert got["stale"] is None
    assert dict(seen[0].url.params) == {"slot": "slot2", "dim": "the_end"}
    started = await mc.minecraft_map_sweep(req, SETTINGS, PRINCIPAL, slot="slot2")
    assert started == {"state": "running"}
    assert seen[1].method == "POST" and json.loads(seen[1].content) == {"slot": "slot2"}


async def test_the_debug_timeline_is_counts_for_the_slots_world(
    sidecar, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seen, replies = sidecar
    replies["/worlds"] = httpx.Response(
        200, json={"slots": [{"id": "slot2", "folder": "castle", "exists": True}]}
    )
    asked: list[tuple[str, float, float]] = []

    async def counts(_maker: Any, world: str, start: float, end: float) -> dict[str, Any]:
        asked.append((world, start, end))
        if end <= start:
            raise ValueError("the range must end after it starts")
        return {"world": world, "players": []}

    monkeypatch.setattr(mc.timeline, "counts", counts)
    req: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=object())),
        state=SimpleNamespace(),
    )
    out = await mc.minecraft_timeline(req, SETTINGS, PRINCIPAL, 10.0, 20.0, slot="slot2")
    assert out == {"world": "castle", "players": []} and asked == [("castle", 10.0, 20.0)]
    assert req.state.debug_detail == "minecraft timeline counts slot2"
    with pytest.raises(HTTPException) as bad:
        await mc.minecraft_timeline(req, SETTINGS, PRINCIPAL, 20.0, 10.0, slot="slot2")
    assert bad.value.status_code == 422
    with pytest.raises(HTTPException) as empty:
        await mc.minecraft_timeline(req, SETTINGS, PRINCIPAL, 10.0, 20.0, slot="slot4")
    assert empty.value.status_code == 404


# ---- test worlds (§SW4): the token makes, resets and empties only its own


def _worlds(*slots: dict[str, Any]) -> httpx.Response:
    base = {"name": None, "exists": False, "active": False, "test": False}
    return httpx.Response(200, json={"slots": [{**base, **s} for s in slots]})


def _writes(seen: list[httpx.Request]) -> list[tuple[str, Any]]:
    return [
        (r.url.path, json.loads(r.content) if r.content else None)
        for r in seen
        if r.method == "POST"
    ]


async def test_a_test_world_is_made_only_in_an_empty_slot_and_marked(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds"] = _worlds({"id": "slot4"}, {"id": "slot2", "name": "Castle Hill"})
    req = _request(FakeSupervisor(RUNNING))
    await mc.minecraft_make_test_world("slot4", mc.TestWorldIn(seed="42"), req, SETTINGS, PRINCIPAL)
    assert _writes(seen) == [
        ("/worlds/slot4/create", {"name": "Test 42", "seed": "42", "wreck": True, "test": True})
    ]
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_make_test_world(
            "slot2", mc.TestWorldIn(seed="42"), req, SETTINGS, PRINCIPAL
        )
    assert exc.value.status_code == 409 and "Castle Hill" in str(exc.value.detail)
    assert len(_writes(seen)) == 1  # the owner's world is never written


async def test_only_a_test_world_can_be_reset_over_the_token(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds"] = _worlds(
        {"id": "slot2", "name": "Castle Hill", "exists": True},
        {"id": "slot4", "name": "Test 42", "exists": True, "test": True},
    )
    req = _request(FakeSupervisor(RUNNING))
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_reset_test_world(
            "slot2", mc.TestResetIn(mode="empty"), req, SETTINGS, PRINCIPAL
        )
    assert exc.value.status_code == 409
    assert _writes(seen) == []
    await mc.minecraft_reset_test_world(
        "slot4", mc.TestResetIn(mode="new_seed", seed="7"), req, SETTINGS, PRINCIPAL
    )
    assert _writes(seen) == [
        ("/worlds/slot4/reset", {"mode": "new_seed", "seed": "7", "wreck": True, "test": True})
    ]


async def test_the_loaded_test_world_waits_for_nobody_to_be_on(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds"] = _worlds({"id": "slot4", "name": "T", "test": True, "active": True})
    replies["/status"] = httpx.Response(200, json={"players": [{"name": "Mira_P"}]})
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_reset_test_world(
            "slot4",
            mc.TestResetIn(mode="same_seed"),
            _request(FakeSupervisor(RUNNING)),
            SETTINGS,
            PRINCIPAL,
        )
    assert exc.value.status_code == 409 and "Mira_P" in str(exc.value.detail)
    assert _writes(seen) == []


async def test_a_load_needs_a_world_and_nobody_playing(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds"] = _worlds({"id": "slot1", "name": "World"}, {"id": "slot5"})
    replies["/status"] = httpx.Response(200, json={"players": []})
    req = _request(FakeSupervisor(RUNNING))
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_load_world("slot5", req, SETTINGS, PRINCIPAL)
    assert exc.value.status_code == 409  # a load would generate a world there
    await mc.minecraft_load_world("slot1", req, SETTINGS, PRINCIPAL)
    assert _writes(seen) == [("/worlds/slot1/load", None)]
    replies["/status"] = httpx.Response(200, json={"players": [{"name": "BlockyFox"}]})
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_load_world("slot1", req, SETTINGS, PRINCIPAL)
    assert exc.value.status_code == 409 and "BlockyFox" in str(exc.value.detail)


async def test_an_idle_test_worlds_reset_doesnt_wait_on_players(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds"] = _worlds({"id": "slot4", "name": "T", "exists": True, "test": True})
    replies["/status"] = httpx.Response(200, json={"players": [{"name": "Mira_P"}]})
    await mc.minecraft_reset_test_world(
        "slot4",
        mc.TestResetIn(mode="empty"),
        _request(FakeSupervisor(RUNNING)),
        SETTINGS,
        PRINCIPAL,
    )
    # Not loaded, so nobody is put out of the game: the reset goes ahead.
    assert [r.url.path for r in seen] == ["/worlds", "/worlds/slot4/reset"]


async def test_a_slot_the_box_doesnt_have_is_a_404(sidecar) -> None:
    _, replies = sidecar
    replies["/worlds"] = _worlds({"id": "slot1", "name": "World"})
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_load_world(
            "slot9", _request(FakeSupervisor(RUNNING)), SETTINGS, PRINCIPAL
        )
    assert exc.value.status_code == 404


async def test_a_made_but_ungenerated_owner_world_is_not_a_free_slot(sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds"] = _worlds({"id": "slot4", "seed": "5127"})  # name blanked
    with pytest.raises(HTTPException) as exc:
        await mc.minecraft_make_test_world(
            "slot4",
            mc.TestWorldIn(seed="9"),
            _request(FakeSupervisor(RUNNING)),
            SETTINGS,
            PRINCIPAL,
        )
    assert exc.value.status_code == 409
    assert _writes(seen) == []
