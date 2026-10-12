"""The PUBLIC Minecraft map-share surface: unauthenticated by design (the token is the
credential), the read side of the owner mint/list/revoke in `minecraft.py`.

Every route resolves `{token}` to a live link first and answers 404 on an unknown or
revoked one. The slot is the link's, never the visitor's: tiles, map info, tapped spots and
structures go to the sidecar for that slot only, and every travel-log read runs under
`mc_share_context(link_id)` so the `*_share` RLS policies (0230) admit that world's history
and nothing else. A slot that now holds a different world (the folder changed) reads as
gone, tiles included (their check is cached for a few seconds). Only reads live here; the
structure sweep and every server control stay owner-only. Responses are rate-limited,
`no-referrer` and `noindex`. This router carries NO owner dependency — do not add owner
reads here.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, cast

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import Response

from jbrain.api import minecraft as owner_api
from jbrain.api.deps import SettingsDep
from jbrain.db.session import mc_share_context
from jbrain.locations.ratelimit import TokenBucket
from jbrain.minecraft import client as mc
from jbrain.minecraft import shares, timeline
from jbrain.minecraft.shares import ShareLink

router = APIRouter(prefix="/mc-share/{token}")

_DIM = r"^(overworld|nether|the_end)$"
_LAYER = r"^(surface|biomes)$"
_COORD = 30_000_000
_PLAYERS = r"^[^,]{1,64}(,[^,]{1,64}){0,15}$"
_PARTS = r"^(trails|events|sessions|explored)(,(trails|events|sessions|explored)){0,3}$"
_HARDEN = {"Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


def _limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.mc_share_rate_limiter)


def _tile_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.mc_share_tile_rate_limiter)


def _maker(request: Request) -> Any:
    return request.app.state.session_maker


def _allow(request: Request, bucket: TokenBucket) -> None:
    client = request.client.host if request.client else "unknown"
    if not bucket.allow(client):
        raise HTTPException(status_code=429, detail="rate limited")


async def _link(request: Request, token: str, *, count_view: bool = False) -> ShareLink:
    link = await shares.resolve_share(_maker(request), token, count_view=count_view)
    if link is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    return link


async def _world(settings: Any, link: ShareLink) -> dict[str, Any]:
    """The link's slot record, or 404 when the slot no longer holds the shared world."""
    try:
        rec = await mc.slot_record(settings, link.slot)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="the shared world is gone") from exc
        raise
    if str(rec.get("folder")) != link.world:
        raise HTTPException(status_code=404, detail="the shared world is gone")
    return rec


# Tiles arrive by the dozen, so each slot's checked folder is kept this long: a map view's
# burst costs one sidecar read, and a world swapped into the slot is gone within it.
FOLDER_TTL = 5.0
# slot -> (expires at, on `_clock`; the folder `_world` last confirmed there)
_folders: dict[str, tuple[float, str]] = {}
_clock = time.monotonic


async def _tile_world(settings: Any, link: ShareLink) -> None:
    """`_world` for tiles: a bearer token outlives the world it was minted for, so an old
    link must never draw whatever world now holds its slot (#1664)."""
    now = _clock()
    cached = _folders.get(link.slot)
    if cached is not None and cached[0] > now:
        if cached[1] != link.world:
            raise HTTPException(status_code=404, detail="the shared world is gone")
        return
    rec = await _world(settings, link)
    _folders[link.slot] = (now + FOLDER_TTL, str(rec.get("folder")))


Token = Annotated[str, Path(min_length=1, max_length=128)]


@router.get("")
async def view(
    token: Token, request: Request, response: Response, settings: SettingsDep
) -> dict[str, Any]:
    """What the viewer opens with: the world and the player the map starts on. The one
    read that counts as a view of the link."""
    _allow(request, _limiter(request))
    response.headers.update(_HARDEN)
    link = await _link(request, token, count_view=True)
    rec = await _world(settings, link)
    return {
        "slot": link.slot,
        "name": rec.get("name"),
        "active": bool(rec.get("active")),
        "last_played": rec.get("last_played"),
        "focus": {"xuid": link.xuid, "gamertag": link.gamertag},
    }


@router.get("/map/info")
async def map_info(
    token: Token,
    request: Request,
    response: Response,
    settings: SettingsDep,
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    _allow(request, _limiter(request))
    response.headers.update(_HARDEN)
    link = await _link(request, token)
    await _world(settings, link)
    return await mc.call(settings, "GET", "/map/info", params={"slot": link.slot, "dim": dim})


@router.get("/map/tile/{dim}/{zoom}/{tx}/{tz}.png")
async def map_tile(
    token: Token,
    dim: Annotated[str, Path(pattern=_DIM)],
    zoom: Annotated[int, Path(ge=0, le=4)],
    tx: int,
    tz: int,
    request: Request,
    settings: SettingsDep,
    layer: Annotated[str, Query(pattern=_LAYER)] = "surface",
) -> Response:
    # A map view pulls dozens of tiles at once, so tiles get their own, wider bucket and
    # a cached world check.
    _allow(request, _tile_limiter(request))
    link = await _link(request, token)
    await _tile_world(settings, link)
    png = await mc.image(
        settings,
        f"/map/tile/{dim}/{zoom}/{tx}/{tz}.png",
        params={"slot": link.slot, "layer": layer},
    )
    return Response(
        png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=60", **_HARDEN},
    )


@router.get("/map/point")
async def map_point(
    token: Token,
    request: Request,
    response: Response,
    settings: SettingsDep,
    x: Annotated[int, Query(ge=-_COORD, le=_COORD)],
    z: Annotated[int, Query(ge=-_COORD, le=_COORD)],
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    _allow(request, _limiter(request))
    response.headers.update(_HARDEN)
    link = await _link(request, token)
    await _world(settings, link)
    params = {"slot": link.slot, "dim": dim, "x": x, "z": z}
    return await mc.call(settings, "GET", "/map/point", params=params)


@router.get("/map/players")
async def map_players(
    token: Token, request: Request, response: Response, settings: SettingsDep
) -> dict[str, Any]:
    _allow(request, _limiter(request))
    response.headers.update(_HARDEN)
    link = await _link(request, token)
    rec = await _world(settings, link)
    return await owner_api.players_view(
        request, settings, link.slot, rec, mc_share_context(link.id)
    )


@router.get("/map/structures")
async def map_structures(
    token: Token,
    request: Request,
    response: Response,
    settings: SettingsDep,
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    _allow(request, _limiter(request))
    response.headers.update(_HARDEN)
    link = await _link(request, token)
    await _world(settings, link)
    params = {"slot": link.slot, "dim": dim}
    return await mc.call(settings, "GET", "/map/structures", params=params)


@router.get("/timeline")
async def map_timeline(
    token: Token,
    request: Request,
    response: Response,
    settings: SettingsDep,
    start: Annotated[float, Query(alias="from")],
    end: Annotated[float, Query(alias="to")],
    players: Annotated[str | None, Query(pattern=_PLAYERS)] = None,
    include: Annotated[str, Query(pattern=_PARTS)] = "trails,events,sessions",
    dim: Annotated[str, Query(pattern=_DIM)] = "overworld",
) -> dict[str, Any]:
    """The owner's `/timeline` for the link's world, read under the share's RLS."""
    _allow(request, _limiter(request))
    response.headers.update(_HARDEN)
    link = await _link(request, token)
    await _world(settings, link)
    try:
        got = await timeline.timeline(
            _maker(request),
            link.world,
            start,
            end,
            players=[p for p in (players or "").split(",") if p],
            include=frozenset(include.split(",")),
            dim=dim,
            ctx=mc_share_context(link.id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"slot": link.slot, **got}
