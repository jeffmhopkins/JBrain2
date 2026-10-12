"""The Minecraft map share: the owner's mint/list/revoke and the PUBLIC read surface.

No DB here: the share store, travel log and sidecar are faked (the RLS that pins a link to
its world is `tests/integration/test_mc_share_pg.py`). Pinned: a link can only read its
own slot whatever the visitor asks for, an unknown token or a slot now holding another
world is a 404, the one page open counts as a view, the travel log is read under the
share's pinned context, no owner action is reachable, and every read is hardened and
rate-limited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import minecraft, minecraft_share
from jbrain.api.deps import owner_only
from jbrain.locations.ratelimit import TokenBucket
from jbrain.minecraft import client as mc
from jbrain.minecraft import shares, targets, timeline, travel
from jbrain.minecraft.shares import ShareLink

WHEN = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
LINK = ShareLink(
    id="link-1",
    slot="slot2",
    world="castle",
    xuid="2535411",
    gamertag="BlockyFox",
    created_at=WHEN,
    revoked_at=None,
    last_viewed_at=None,
    view_count=0,
)
WORLDS = {
    "slots": [
        {"id": "slot1", "folder": "Bedrock level", "name": "Home", "exists": True},
        {
            "id": "slot2",
            "folder": "castle",
            "name": "Castle Hill",
            "exists": True,
            "active": True,
            "last_played": 1_791_000_000,
        },
    ]
}
POSITIONS = [
    {"xuid": "2535411", "gamertag": "BlockyFox", "dim": "overworld", "x": 1, "y": 2, "z": 3},
    {"xuid": "2535412", "gamertag": "Mira_P", "dim": "nether", "x": 4, "y": 5, "z": 6},
]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {
        "calls": [],
        "images": [],
        "resolved": [],
        "positions": [],
        "timelines": [],
        "worlds": WORLDS,
        "link": LINK,
        "minted": [],
        "revoked": [],
        "locks": [],
    }

    async def call(_s: Any, method: str, path: str, **kw: Any) -> dict[str, Any]:
        state["calls"].append((method, path, kw.get("params")))
        if path == "/worlds":
            if isinstance(state["worlds"], Exception):
                raise state["worlds"]
            return state["worlds"]
        if path == "/status":
            return {"state": "running", "players": [{"xuid": "2535411", "name": "BlockyFox"}]}
        return {"path": path}

    async def image(_s: Any, path: str, params: dict[str, Any] | None = None) -> bytes:
        state["images"].append((path, params))
        return b"\x89PNG"

    async def resolve(_m: Any, token: str, *, count_view: bool = False) -> ShareLink | None:
        state["resolved"].append((token, count_view))
        return state["link"] if token == "good" else None

    async def latest(_m: Any, world: str, ctx: Any = None) -> list[dict[str, Any]]:
        state["positions"].append((world, ctx))
        return [dict(p) for p in POSITIONS]

    async def tl(_m: Any, world: str, start: float, end: float, **kw: Any) -> dict[str, Any]:
        state["timelines"].append((world, start, end, kw))
        return {"world": world, "trails": [], "events": [], "sessions": []}

    async def mint(_m: Any, ctx: Any, **kw: Any) -> tuple[str, ShareLink]:
        state["minted"].append((ctx, kw))
        return "secret", ShareLink(**{**LINK.__dict__, **kw})

    async def listed(_m: Any, _ctx: Any) -> list[ShareLink]:
        return [LINK]

    async def revoke(_m: Any, _ctx: Any, share_id: str) -> bool:
        state["revoked"].append(share_id)
        return share_id == "link-1"

    monkeypatch.setattr(mc, "call", call)
    monkeypatch.setattr(mc, "image", image)
    monkeypatch.setattr(shares, "resolve_share", resolve)
    monkeypatch.setattr(shares, "mint_share", mint)
    monkeypatch.setattr(shares, "list_shares", listed)
    monkeypatch.setattr(shares, "revoke_share", revoke)
    monkeypatch.setattr(travel, "latest_positions", latest)

    async def locks(_m: Any, world: str, ctx: Any = None) -> dict[str, Any]:
        state["locks"].append((world, ctx))
        return {"mira_p": {"dim": "nether", "x": 1, "y": None, "z": 2, "label": "Fortress"}}

    monkeypatch.setattr(timeline, "timeline", tl)
    monkeypatch.setattr(targets, "for_world", locks)
    # Each test starts with no slot folder cached, at a clock it can move.
    monkeypatch.setattr(minecraft_share, "_folders", {})
    state["now"] = 1000.0
    monkeypatch.setattr(minecraft_share, "_clock", lambda: state["now"])
    return state


def _asked(fake: dict[str, Any]) -> list[Any]:
    # Only the sidecar reads these routes make: another test's leftover drain loop may
    # still be polling the faked sidecar's /events.
    return [c for c in fake["calls"] if c[1] in ("/worlds", "/status") or c[1].startswith("/map")]


def _client(*, owner: bool = False, capacity: float = 60) -> TestClient:
    app = FastAPI()
    app.include_router(minecraft_share.router, prefix="/api")
    app.include_router(minecraft.router, prefix="/api")
    app.state.session_maker = object()
    app.state.settings = SimpleNamespace(minecraft_url="http://x", minecraft_token="t")
    app.state.mc_share_rate_limiter = TokenBucket(capacity=capacity, refill_per_sec=0.0)
    app.state.mc_share_tile_rate_limiter = TokenBucket(capacity=capacity, refill_per_sec=0.0)
    if owner:
        app.dependency_overrides[owner_only] = lambda: SimpleNamespace(id="owner-pid")
    else:

        def deny() -> None:
            raise HTTPException(status_code=401, detail="not authenticated")

        app.dependency_overrides[owner_only] = deny
    return TestClient(app)


def _hardened(resp: Any) -> None:
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "noindex" in resp.headers["X-Robots-Tag"]


def test_the_page_open_names_the_world_and_player_and_counts_a_view(fake) -> None:
    resp = _client().get("/api/mc-share/good")
    assert resp.status_code == 200
    assert resp.json() == {
        "slot": "slot2",
        "name": "Castle Hill",
        "active": True,
        "last_played": 1_791_000_000,
        "focus": {"xuid": "2535411", "gamertag": "BlockyFox"},
    }
    _hardened(resp)
    assert fake["resolved"] == [("good", True)]


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/map/info",
        "/map/players",
        "/map/structures",
        "/map/point?x=1&z=2",
        "/map/tile/overworld/0/0/0.png",
        "/timeline?from=0&to=10",
    ],
)
def test_an_unknown_or_revoked_token_reads_nothing(fake, path: str) -> None:
    assert _client().get(f"/api/mc-share/nope{path}").status_code == 404
    assert _asked(fake) == [] and fake["images"] == [] and fake["positions"] == []


def test_a_slot_now_holding_another_world_is_gone(fake) -> None:
    fake["worlds"] = {"slots": [{**WORLDS["slots"][1], "folder": "castle-reset-2"}]}
    client = _client()
    for path in (
        "",
        "/map/info",
        "/map/players",
        "/timeline?from=0&to=10",
        "/map/tile/overworld/0/0/0.png",
    ):
        resp = client.get(f"/api/mc-share/good{path}")
        assert resp.status_code == 404 and resp.json()["detail"] == "the shared world is gone"
    fake["worlds"] = {"slots": []}
    assert client.get("/api/mc-share/good").json()["detail"] == "the shared world is gone"
    tile = client.get("/api/mc-share/good/map/tile/overworld/0/0/0.png")
    assert tile.status_code == 404 and tile.json()["detail"] == "the shared world is gone"
    assert fake["images"] == []  # no other world's tile ever leaves the box


def _worlds_read(fake: dict[str, Any]) -> int:
    return sum(1 for c in fake["calls"] if c[1] == "/worlds")


def test_tiles_check_the_world_once_per_burst_and_again_after_it_lapses(fake) -> None:
    client = _client()
    tile = "/api/mc-share/good/map/tile/overworld/0/{}/0.png"
    for x in range(5):
        assert client.get(tile.format(x)).status_code == 200
    assert _worlds_read(fake) == 1 and len(fake["images"]) == 5

    # Another world in the slot: still the cached answer until it lapses, then gone.
    fake["worlds"] = {"slots": [{**WORLDS["slots"][1], "folder": "castle-reset-2"}]}
    fake["now"] += minecraft_share.FOLDER_TTL - 0.1
    assert client.get(tile.format(9)).status_code == 200
    fake["now"] += 0.1
    assert client.get(tile.format(9)).status_code == 404
    assert _worlds_read(fake) == 2 and len(fake["images"]) == 6


def test_a_cached_slot_holding_another_world_answers_gone_without_the_sidecar(fake) -> None:
    client = _client()
    assert client.get("/api/mc-share/good/map/tile/overworld/0/0/0.png").status_code == 200
    # An older link to the same slot, minted for the world that was there before.
    fake["link"] = ShareLink(**{**LINK.__dict__, "world": "castle-old"})
    resp = client.get("/api/mc-share/good/map/tile/overworld/0/0/0.png")
    assert resp.status_code == 404 and resp.json()["detail"] == "the shared world is gone"
    assert _worlds_read(fake) == 1 and len(fake["images"]) == 1


def test_the_sidecar_reads_are_pinned_to_the_links_slot(fake) -> None:
    client = _client()
    # A visitor-supplied slot is not a parameter of these routes; it is ignored.
    assert client.get("/api/mc-share/good/map/info?dim=nether&slot=slot1").status_code == 200
    assert client.get("/api/mc-share/good/map/structures?slot=slot1").status_code == 200
    assert client.get("/api/mc-share/good/map/point?x=5&z=-7").status_code == 200
    asked = [(p, q) for _m, p, q in fake["calls"] if p.startswith("/map")]
    assert asked == [
        ("/map/info", {"slot": "slot2", "dim": "nether"}),
        ("/map/structures", {"slot": "slot2", "dim": "overworld"}),
        ("/map/point", {"slot": "slot2", "dim": "overworld", "x": 5, "z": -7}),
    ]
    resp = client.get("/api/mc-share/good/map/tile/the_end/3/-2/4.png?layer=biomes&slot=slot1")
    assert resp.status_code == 200 and resp.content == b"\x89PNG"
    assert resp.headers["content-type"] == "image/png"
    _hardened(resp)
    assert fake["images"] == [
        ("/map/tile/the_end/3/-2/4.png", {"slot": "slot2", "layer": "biomes"})
    ]
    # Only the page open counts a view, not each read behind it.
    assert {v for _t, v in fake["resolved"]} == {False}


def test_players_and_the_timeline_read_under_the_shares_context(fake) -> None:
    client = _client()
    resp = client.get("/api/mc-share/good/map/players")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is True and body["slot"] == "slot2"
    assert [(p["gamertag"], p["online"]) for p in body["players"]] == [
        ("BlockyFox", True),
        ("Mira_P", False),
    ]
    _hardened(resp)
    # The share sees its players' Tricorder targets, read under its own context too.
    assert [p["target"] for p in body["players"]][1]["label"] == "Fortress"
    assert body["players"][0]["target"] is None
    ((_, lock_ctx),) = fake["locks"]
    assert lock_ctx.auth_context == "mc_share" and lock_ctx.principal_id == "link-1"
    ((world, ctx),) = fake["positions"]
    assert world == "castle"
    assert (ctx.auth_context, ctx.principal_id, ctx.principal_kind) == (
        "mc_share",
        "link-1",
        "mc_share",
    )

    resp = client.get(
        "/api/mc-share/good/timeline?from=10&to=20&players=2535412&include=trails,explored"
    )
    assert resp.status_code == 200 and resp.json()["slot"] == "slot2"
    ((world, start, end, kw),) = fake["timelines"]
    assert (world, start, end) == ("castle", 10.0, 20.0)
    assert kw["players"] == ["2535412"]
    assert kw["include"] == frozenset({"trails", "explored"})
    assert kw["ctx"].principal_id == "link-1" and kw["ctx"].auth_context == "mc_share"


def test_a_bad_timeline_range_is_a_422(fake, monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise ValueError("range is backwards")

    monkeypatch.setattr(timeline, "timeline", refuse)
    resp = _client().get("/api/mc-share/good/timeline?from=20&to=10")
    assert resp.status_code == 422 and resp.json()["detail"] == "range is backwards"


def test_no_owner_action_is_reachable_with_a_token(fake) -> None:
    client = _client()
    assert client.post("/api/mc-share/good/map/structures/sweep").status_code in (404, 405)
    assert client.get("/api/mc-share/good/worlds").status_code == 404
    assert client.post("/api/mc-share/good").status_code == 405
    # The owner routes themselves still need the owner.
    assert client.get("/api/minecraft/shares").status_code == 401
    assert client.post("/api/minecraft/map/structures/sweep").status_code == 401
    assert _asked(fake) == []


def test_reads_are_rate_limited(fake) -> None:
    client = _client(capacity=2)
    assert client.get("/api/mc-share/good/map/info").status_code == 200
    assert client.get("/api/mc-share/good/map/info").status_code == 200
    assert client.get("/api/mc-share/good/map/info").status_code == 429
    # Tiles draw from their own bucket.
    assert client.get("/api/mc-share/good/map/tile/overworld/0/0/0.png").status_code == 200


def test_the_owner_mints_a_link_for_a_player_seen_in_the_world(fake) -> None:
    resp = _client(owner=True).post(
        "/api/minecraft/shares", json={"slot": "slot2", "xuid": "2535412"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token"] == "secret" and body["gamertag"] == "Mira_P"
    assert body["slot"] == "slot2" and body["view_count"] == 0
    ((ctx, kw),) = fake["minted"]
    assert ctx.principal_id == "owner-pid" and ctx.principal_kind == "owner"
    assert kw == {"slot": "slot2", "world": "castle", "xuid": "2535412", "gamertag": "Mira_P"}


def test_minting_refuses_a_stranger_or_an_empty_slot(fake) -> None:
    client = _client(owner=True)
    resp = client.post("/api/minecraft/shares", json={"slot": "slot2", "xuid": "999"})
    assert resp.status_code == 404
    fake["worlds"] = {"slots": []}
    resp = client.post("/api/minecraft/shares", json={"slot": "slot2", "xuid": "2535411"})
    assert resp.status_code == 404
    assert (
        client.post("/api/minecraft/shares", json={"slot": "../x", "xuid": "1"}).status_code == 422
    )
    assert fake["minted"] == []


def test_the_owner_lists_and_revokes(fake) -> None:
    client = _client(owner=True)
    (row,) = client.get("/api/minecraft/shares").json()
    assert row["id"] == "link-1" and row["gamertag"] == "BlockyFox"
    assert row["created_at"] == WHEN.timestamp() and row["last_viewed_at"] is None
    assert "token" not in row
    assert client.delete("/api/minecraft/shares/link-1").status_code == 204
    assert client.delete("/api/minecraft/shares/nope").status_code == 404
    assert fake["revoked"] == ["link-1", "nope"]


def test_an_unreachable_box_is_not_called_a_gone_world(fake) -> None:
    fake["worlds"] = HTTPException(status_code=502, detail="Minecraft sidecar unreachable")
    resp = _client().get("/api/mc-share/good")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Minecraft sidecar unreachable"
