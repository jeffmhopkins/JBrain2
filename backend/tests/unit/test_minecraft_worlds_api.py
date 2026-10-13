"""The owner's world and backup routes (MINECRAFT_BEDROCK_PLAN §M2/§M3).

Pinned: an upload streams through to the sidecar with its length and name and is never
held by the api; a download streams back as an attachment; a sidecar refusal (a busy
lifecycle, an invalid world) arrives as what it is; path parameters can't smuggle a
traversal; server-wide settings are a fixed set mapped onto server.properties keys.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import minecraft as api
from jbrain.api.deps import owner_only
from jbrain.minecraft import client as mc

SETTINGS: Any = SimpleNamespace(minecraft_url="http://mc", minecraft_token="tok")


@pytest.fixture
def sidecar(monkeypatch: pytest.MonkeyPatch):
    seen: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return replies.get(request.url.path, httpx.Response(200, json={"ok": True}))

    monkeypatch.setattr(mc, "_transport", httpx.MockTransport(handler))
    return seen, replies


@pytest.fixture
def client(sidecar) -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[owner_only] = lambda: SimpleNamespace(id="owner")
    app.state.settings = SETTINGS
    return TestClient(app)


def test_an_upload_streams_through_with_its_length_and_name(client, sidecar) -> None:
    seen, _ = sidecar
    blob = b"PK\x03\x04" + b"x" * 5000
    resp = client.post(
        "/api/minecraft/worlds/slot2/import?name=Castle",
        content=blob,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    up = seen[-1]
    assert up.url.path == "/worlds/slot2/import"
    assert up.url.params["name"] == "Castle"
    assert up.headers["authorization"] == "Bearer tok"
    assert up.content == blob


def test_an_invalid_world_is_a_400_with_the_sidecars_reason(client, sidecar) -> None:
    _, replies = sidecar
    replies["/worlds/slot2/import"] = httpx.Response(
        400, json={"detail": "that isn't a Bedrock world (no level.dat inside)"}
    )
    resp = client.post("/api/minecraft/worlds/slot2/import", content=b"nope")
    assert resp.status_code == 400 and "level.dat" in resp.json()["detail"]


def test_a_busy_server_refuses_with_409(client, sidecar) -> None:
    _, replies = sidecar
    replies["/worlds/slot3/load"] = httpx.Response(409, json={"detail": "busy: updating"})
    resp = client.post("/api/minecraft/worlds/slot3/load")
    assert resp.status_code == 409 and "updating" in resp.json()["detail"]


def test_a_download_streams_back_as_an_attachment(client, sidecar) -> None:
    _, replies = sidecar
    name = "world-20261010-120000-mine.mcworld"
    replies[f"/snapshots/{name}/file"] = httpx.Response(
        200, content=b"zipbytes", headers={"content-length": "8"}
    )
    resp = client.get(f"/api/minecraft/backups/{name}/file")
    assert resp.status_code == 200 and resp.content == b"zipbytes"
    assert name in resp.headers["content-disposition"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/minecraft/worlds/..%2Fetc/load",
        "/api/minecraft/worlds/world/load",
        "/api/minecraft/backups/..%2F..%2Fsecret/file",
        "/api/minecraft/backups/notabackup.zip/file",
    ],
)
def test_path_params_cannot_smuggle_a_traversal(client, sidecar, path: str) -> None:
    seen, _ = sidecar
    method = client.post if path.endswith("/load") else client.get
    assert method(path).status_code in (404, 422)
    assert seen == []


def test_restore_targets_a_slot_and_reset_carries_its_mode(client, sidecar) -> None:
    seen, _ = sidecar
    name = "world-20261010-120000-mine.mcworld"
    client.post(f"/api/minecraft/backups/{name}/restore", json={"slot": "slot3"})
    client.post("/api/minecraft/worlds/slot2/reset", json={"mode": "same_seed"})
    assert json.loads(seen[0].content) == {"slot": "slot3"}
    assert json.loads(seen[1].content) == {"mode": "same_seed", "seed": None}


def test_server_settings_are_a_fixed_set_on_server_properties(client, sidecar) -> None:
    seen, replies = sidecar
    replies["/properties"] = httpx.Response(
        200,
        json={
            "effective": {
                "server-name": "JBrain",
                "max-players": "10",
                "view-distance": "32",
                "level-name": "world",
            }
        },
    )
    resp = client.put(
        "/api/minecraft/server-settings", json={"server_name": "Hopkins", "max_players": 6}
    )
    assert resp.status_code == 200
    assert (resp.json()["max_players"], resp.json()["view_distance"]) == (10, 32)  # numbers
    sent = next(r for r in seen if r.method == "POST" and r.url.path == "/properties")
    assert json.loads(sent.content) == {"set": {"server-name": "Hopkins", "max-players": "6"}}
    assert (
        client.put("/api/minecraft/server-settings", json={"max_players": 500}).status_code == 422
    )


def test_rules_and_seed_routes_pass_through(client, sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds/new-seed"] = httpx.Response(200, json={"seed": "123"})
    assert client.get("/api/minecraft/worlds/new-seed").json() == {"seed": "123"}
    client.put("/api/minecraft/worlds/slot2/rules", json={"set": {"doFireTick": False}})
    sent = next(r for r in seen if r.url.path == "/worlds/slot2/rules")
    assert json.loads(sent.content) == {"set": {"doFireTick": False}}
    client.post(
        "/api/minecraft/worlds/slot3/create",
        json={"name": "Peaceful", "seed": "glacier", "rules": {"keepInventory": True}},
    )
    created = next(r for r in seen if r.url.path == "/worlds/slot3/create")
    assert json.loads(created.content)["rules"] == {"keepInventory": True}
    assert (
        client.post("/api/minecraft/worlds/slot3/create", json={"seed": "x" * 65}).status_code
        == 422
    )


def test_world_input_is_checked_before_it_reaches_the_box(client, sidecar) -> None:
    bad = [
        ("post", "/api/minecraft/worlds/slot2/reset", {"mode": "new_seed", "seed": "x" * 65}),
        ("post", "/api/minecraft/worlds/slot2/reset", {"mode": "wipe"}),
        ("post", "/api/minecraft/worlds/slot2/create", {"gamemode": "spectator"}),
        ("post", "/api/minecraft/worlds/slot2/create", {"name": "n" * 41}),
        ("patch", "/api/minecraft/worlds/slot2", {"difficulty": "nightmare"}),
    ]
    for method, path, body in bad:
        assert getattr(client, method)(path, json=body).status_code == 422, (path, body)


def test_map_tiles_pass_through_as_png_and_bad_input_never_reaches_the_box(client, sidecar) -> None:
    seen, replies = sidecar
    png = b"\x89PNG\r\n\x1a\nfake"
    replies["/map/tile/overworld/1/-2/3.png"] = httpx.Response(200, content=png)
    replies["/map/info"] = httpx.Response(200, json={"extent": None, "tile_blocks": 256})
    resp = client.get("/api/minecraft/map/tile/overworld/1/-2/3.png?slot=slot2")
    assert resp.status_code == 200 and resp.content == png
    assert resp.headers["content-type"] == "image/png"
    assert seen[-1].url.params["slot"] == "slot2"
    assert seen[-1].url.params["layer"] == "surface"  # true colour unless asked otherwise
    client.get("/api/minecraft/map/tile/overworld/1/-2/3.png?layer=biomes")
    assert seen[-1].url.params["layer"] == "biomes"
    info = client.get("/api/minecraft/map/info?dim=nether").json()
    assert info["tile_blocks"] == 256 and seen[-1].url.params["dim"] == "nether"
    count = len(seen)
    for bad in (
        "/api/minecraft/map/tile/aether/0/0/0.png",
        "/api/minecraft/map/tile/overworld/5/0/0.png",
        "/api/minecraft/map/tile/overworld/0/0/0.png?slot=../etc",
        "/api/minecraft/map/tile/overworld/0/0/0.png?layer=../etc",
        "/api/minecraft/map/info?dim=moon",
    ):
        assert client.get(bad).status_code == 422, bad
    assert len(seen) == count


def test_a_refused_tile_is_the_boxs_reason(client, sidecar) -> None:
    _seen, replies = sidecar
    replies["/map/tile/overworld/0/0/0.png"] = httpx.Response(
        400, json={"detail": "slot2 has no world yet"}
    )
    resp = client.get("/api/minecraft/map/tile/overworld/0/0/0.png?slot=slot2")
    assert resp.status_code == 400 and "no world" in resp.json()["detail"]


def test_a_tapped_spot_passes_through_with_the_debug_twins_bounds(client, sidecar) -> None:
    seen, replies = sidecar
    replies["/map/point"] = httpx.Response(
        200, json={"source": "survey", "biome": "Cherry Grove", "y": 129}
    )
    got = client.get("/api/minecraft/map/point?x=212&z=-341&slot=slot2&dim=nether").json()
    assert got["biome"] == "Cherry Grove"
    assert dict(seen[-1].url.params) == {"slot": "slot2", "dim": "nether", "x": "212", "z": "-341"}
    count = len(seen)
    for bad in (
        "/api/minecraft/map/point?x=1",
        "/api/minecraft/map/point?x=30000001&z=0",
        "/api/minecraft/map/point?x=0&z=0&dim=moon",
        "/api/minecraft/map/point?x=0&z=0&slot=../x",
    ):
        assert client.get(bad).status_code == 422, bad
    assert len(seen) == count


def test_structures_and_a_sweep_pass_through_and_a_refusal_is_a_409(client, sidecar) -> None:
    seen, replies = sidecar
    replies["/map/structures"] = httpx.Response(
        200,
        json={"structures": [{"structure": "village", "x": 232, "z": -304}], "stale": None},
    )
    got = client.get("/api/minecraft/map/structures?slot=slot2&dim=overworld").json()
    assert got["structures"][0]["structure"] == "village"
    assert dict(seen[-1].url.params) == {"slot": "slot2", "dim": "overworld"}
    replies["/map/structures/sweep"] = httpx.Response(
        409, json={"detail": "only the loaded world can be searched — load it first"}
    )
    resp = client.post("/api/minecraft/map/structures/sweep?slot=slot3")
    assert resp.status_code == 409 and "loaded world" in resp.json()["detail"]
    assert json.loads(seen[-1].content) == {"slot": "slot3"}
    assert client.get("/api/minecraft/map/structures?dim=moon").status_code == 422


def _players_world(sidecar, *, active: bool, state: str = "running") -> None:
    _seen, replies = sidecar
    replies["/worlds"] = httpx.Response(
        200,
        json={
            "slots": [
                {"id": "slot2", "folder": "castle", "exists": True, "active": active},
                {"id": "slot5", "folder": "slot5", "exists": False, "active": False},
            ]
        },
    )
    replies["/status"] = httpx.Response(
        200, json={"state": state, "players": [{"name": "BlockyFox", "xuid": "2535"}]}
    )


def test_map_players_are_the_travel_logs_latest_with_who_is_on(
    client, sidecar, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    async def latest(_maker: Any, world: str) -> list[dict[str, Any]]:
        asked.append(world)
        return [
            {
                "xuid": "2535",
                "gamertag": "BlockyFox",
                "dim": "overworld",
                "x": 1.0,
                "y": 64.0,
                "z": 2.0,
                "at": 10.0,
            },
            {
                "xuid": "999",
                "gamertag": "Steve42",
                "dim": "nether",
                "x": 3.0,
                "y": 70.0,
                "z": 4.0,
                "at": 5.0,
            },
        ]

    lock = {"dim": "overworld", "x": 40, "y": None, "z": -108, "label": "Fortress", "set_at": 9.0}

    async def locks(_m: Any, world: str, _ctx: Any = None) -> dict[str, Any]:
        return {"steve42": lock} if world == "castle" else {}

    scan = {
        "gamertag": "BlockyFox",
        "label": "diamond",
        "ids": ["minecraft:diamond_ore"],
        "state": "found",
        "found": {"dim": "overworld", "x": 9, "y": 50, "z": 2},
        "at": 8.0,
        "set_at": 1.0,
    }

    async def scans(_m: Any, world: str, _ctx: Any = None) -> dict[str, Any]:
        return {"blockyfox": scan} if world == "castle" else {}

    monkeypatch.setattr(api.travel, "latest_positions", latest)
    monkeypatch.setattr(api.targets, "for_world", locks)
    monkeypatch.setattr(api.livescan, "for_world", scans)
    client.app.state.session_maker = object()
    _players_world(sidecar, active=True)
    got = client.get("/api/minecraft/map/players?slot=slot2").json()
    assert asked == ["castle"] and got["live"] is True and got["world"] == "castle"
    assert [(p["gamertag"], p["online"]) for p in got["players"]] == [
        ("BlockyFox", True),
        ("Steve42", False),
    ]
    # Each player carries the Tricorder target Dave set, matched by gamertag, or null.
    assert [p["target"] for p in got["players"]] == [None, lock]
    # And the live scan pointing it now (§M25), kept beside the waypoint, never over it.
    assert [p["scan"] for p in got["players"]] == [
        {k: scan[k] for k in ("label", "state", "found", "at", "set_at")},
        None,
    ]
    _players_world(sidecar, active=True, state="stopped")
    stopped = client.get("/api/minecraft/map/players?slot=slot2").json()
    assert stopped["live"] is False and not any(p["online"] for p in stopped["players"])
    _players_world(sidecar, active=False)
    other = client.get("/api/minecraft/map/players?slot=slot2").json()
    assert other["live"] is False and not any(p["online"] for p in other["players"])
    assert client.get("/api/minecraft/map/players?slot=slot5").status_code == 404


def test_map_markers_read_the_slots_world_whether_or_not_it_is_loaded(
    client, sidecar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§M26: the drain's rows for the slot's world, in one dimension or all, with the
    server stopped too — the rows outlive the session."""
    asked: list[tuple[str, str | None]] = []
    rows = {"beacons": [{"dim": "overworld", "x": 1}], "gates": [], "as_of": 7.0}

    async def read(_m: Any, world: str, dim: str | None = None) -> dict[str, Any]:
        asked.append((world, dim))
        return rows

    monkeypatch.setattr(api.markers, "for_world", read)
    client.app.state.session_maker = object()
    _players_world(sidecar, active=False, state="stopped")
    got = client.get("/api/minecraft/map/markers?slot=slot2").json()
    assert got == {"slot": "slot2", "world": "castle", **rows}
    client.get("/api/minecraft/map/markers?slot=slot2&dim=nether")
    assert asked == [("castle", None), ("castle", "nether")]
    assert client.get("/api/minecraft/map/markers?slot=slot2&dim=moon").status_code == 422
    assert client.get("/api/minecraft/map/markers?slot=slot5").status_code == 404


def test_the_timeline_reads_the_slots_world_with_its_range_and_filters(
    client, sidecar, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[dict[str, Any]] = []

    async def timeline(_maker: Any, world: str, start: float, end: float, **kw: Any):
        asked.append({"world": world, "start": start, "end": end, **kw})
        if end - start > 1_000:
            raise ValueError("a range covers at most 92 days")
        return {"world": world, "trails": [], "events": [], "sessions": []}

    monkeypatch.setattr(api.timeline, "timeline", timeline)
    client.app.state.session_maker = object()
    _players_world(sidecar, active=False)
    got = client.get(
        "/api/minecraft/timeline?slot=slot2&from=100&to=700&players=2535,name:Mira P"
        "&include=sessions,explored&dim=nether"
    )
    assert got.status_code == 200 and got.json()["slot"] == "slot2"
    assert asked[-1] == {
        "world": "castle",
        "start": 100.0,
        "end": 700.0,
        "players": ["2535", "name:Mira P"],
        "include": frozenset({"sessions", "explored"}),
        "dim": "nether",
    }
    plain = client.get("/api/minecraft/timeline?slot=slot2&from=0&to=10")
    assert plain.status_code == 200
    assert asked[-1]["players"] == []
    assert asked[-1]["include"] == frozenset({"trails", "events", "sessions"})
    too_long = client.get("/api/minecraft/timeline?slot=slot2&from=0&to=5000")
    assert too_long.status_code == 422 and "92 days" in too_long.json()["detail"]
    assert client.get("/api/minecraft/timeline?slot=slot5&from=0&to=1").status_code == 404
    many = ",".join(["a"] * 17)
    for bad in ("include=positions", "dim=moon", f"players={many}"):
        query = f"slot=slot2&from=0&to=1&{bad}"
        assert client.get(f"/api/minecraft/timeline?{query}").status_code == 422, bad
    assert client.get("/api/minecraft/timeline?slot=slot2&from=x&to=1").status_code == 422
    assert client.get("/api/minecraft/timeline?slot=slot2&to=1").status_code == 422


def test_the_power_pack_switch_reaches_the_box_and_is_checked_first(client, sidecar) -> None:
    seen, replies = sidecar
    replies["/worlds/slot2/power-pack"] = httpx.Response(
        200, json={"id": "slot2", "power_pack_keep": True, "applies": "next load"}
    )
    resp = client.put("/api/minecraft/worlds/slot2/power-pack", json={"on": True})
    assert resp.status_code == 200 and resp.json()["power_pack_keep"] is True
    sent = seen[-1]
    assert (sent.method, sent.url.path) == ("POST", "/worlds/slot2/power-pack")
    assert json.loads(sent.content) == {"on": True}
    count = len(seen)
    assert client.put("/api/minecraft/worlds/slot2/power-pack", json={}).status_code == 422
    assert client.put("/api/minecraft/worlds/../power-pack", json={"on": True}).status_code in (
        404,
        422,
    )
    assert len(seen) == count  # refused before the box


def test_a_rule_the_charm_holds_is_refused_with_the_boxs_reason(client, sidecar) -> None:
    _, replies = sidecar
    replies["/worlds/slot2/rules"] = httpx.Response(
        400, json={"detail": "keepInventory is managed by the Power Pack charm while it is on"}
    )
    resp = client.put("/api/minecraft/worlds/slot2/rules", json={"set": {"keepInventory": False}})
    assert resp.status_code == 400 and "Power Pack charm" in resp.json()["detail"]


def test_map_places_read_the_slots_world_owner_only(
    client, sidecar, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI gate `minecraft-map-layers`, places A: every player's places in the slot's
    world, with the server stopped too."""
    asked: list[tuple[str, str | None]] = []
    rows = {"places": [{"name": "Home"}], "as_of": 7.0}

    async def read(_m: Any, world: str, dim: str | None = None) -> dict[str, Any]:
        asked.append((world, dim))
        return rows

    monkeypatch.setattr(api.places, "for_world", read)
    client.app.state.session_maker = object()
    _players_world(sidecar, active=False, state="stopped")
    got = client.get("/api/minecraft/map/places?slot=slot2").json()
    assert got == {"slot": "slot2", "world": "castle", **rows}
    client.get("/api/minecraft/map/places?slot=slot2&dim=nether")
    assert asked == [("castle", None), ("castle", "nether")]
    assert client.get("/api/minecraft/map/places?slot=slot2&dim=moon").status_code == 422
    assert client.get("/api/minecraft/map/places?slot=slot5").status_code == 404


def test_map_entities_and_a_villagers_card_pass_through_checked(client, sidecar) -> None:
    """§M4e on the map: the sidecar's listing, in a dimension or a box, and one
    villager's trades by its id; a half box or a bad id never reaches the box."""
    seen, replies = sidecar
    replies["/map/entities"] = httpx.Response(200, json={"villages": [], "live": False})
    got = client.get("/api/minecraft/map/entities?slot=slot2&dim=nether")
    assert got.json() == {"villages": [], "live": False}
    assert dict(seen[-1].url.params) == {"slot": "slot2", "dim": "nether"}
    client.get("/api/minecraft/map/entities?slot=slot2&x0=-10&z0=-20&x1=30&z1=40")
    assert dict(seen[-1].url.params) == {
        "slot": "slot2",
        "dim": "overworld",
        "x0": "-10",
        "z0": "-20",
        "x1": "30",
        "z1": "40",
    }
    n = len(seen)
    assert client.get("/api/minecraft/map/entities?x0=1&z0=2").status_code == 422
    assert client.get("/api/minecraft/map/entities?dim=moon").status_code == 422
    replies["/map/villagers"] = httpx.Response(200, json={"found": None})
    assert client.get("/api/minecraft/map/villager?slot=slot2&id=-858").json() == {"found": None}
    assert dict(seen[-1].url.params) == {"slot": "slot2", "id": "-858"}
    client.get("/api/minecraft/map/villager?id=at:0:-3:64:7")
    assert seen[-1].url.params["id"] == "at:0:-3:64:7"
    m = len(seen)
    for bad in ("1;op", "at:0", "", "x" * 30):
        assert client.get(f"/api/minecraft/map/villager?id={bad}").status_code == 422
    assert len(seen) == m and n == m - 2
