"""The basemap tile proxy endpoint (owner-only).

The location map's Leaflet layer points here, so tiles reach the phone only from
this box. Owner-gated: only the owner's session triggers an upstream fetch, and a
miss/disabled/out-of-range request 404s so the map degrades to the schematic.
"""

from typing import cast

from fastapi import APIRouter, Depends, Request, Response

from jbrain.api.deps import owner_only
from jbrain.tiles import TileService

router = APIRouter(prefix="/tiles", dependencies=[Depends(owner_only)])

# Tiles are stable map data; let the browser + service worker hold them so a pan
# back doesn't re-hit the proxy. 30 days, like a hashed asset but not immutable.
_CACHE_CONTROL = "private, max-age=2592000"


def get_tile_service(request: Request) -> TileService:
    return cast(TileService, request.app.state.tile_service)


@router.get("/{z}/{x}/{y}.png")
async def tile(request: Request, z: int, x: int, y: int) -> Response:
    data = await get_tile_service(request).tile(z, x, y)
    if data is None:
        return Response(status_code=404)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": _CACHE_CONTROL})
