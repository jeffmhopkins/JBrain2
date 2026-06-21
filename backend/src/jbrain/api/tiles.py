"""The basemap tile proxy endpoint (any authenticated session).

The location map's Leaflet layer points here, so tiles reach the phone only from
this box — never a third-party tile host. The scheme path segment (`dark`/`light`)
selects the basemap style; each scheme has its own upstream + cache, so the app's
light/dark toggle never mixes the two styles' cached tiles. Gated to a valid session
(owner OR a member/device cookie) so the member dashboard's map renders too; the
basemap is public OSM imagery, while the sensitive fixes stay RLS-scoped elsewhere.
An anonymous request 401s; an unknown-scheme / miss / disabled / out-of-range request
404s so the map degrades to the schematic.
"""

from typing import cast

from fastapi import APIRouter, Depends, Request, Response

from jbrain.api.deps import current_principal
from jbrain.tiles import TileSet

router = APIRouter(prefix="/tiles", dependencies=[Depends(current_principal)])

# Tiles are stable map data; let the browser + service worker hold them so a pan
# back doesn't re-hit the proxy. 30 days, like a hashed asset but not immutable.
_CACHE_CONTROL = "private, max-age=2592000"


def get_tile_set(request: Request) -> TileSet:
    return cast(TileSet, request.app.state.tile_set)


@router.get("/{scheme}/{z}/{x}/{y}.png")
async def tile(request: Request, scheme: str, z: int, x: int, y: int) -> Response:
    service = get_tile_set(request).service(scheme)
    if service is None:
        return Response(status_code=404)  # unknown scheme
    data = await service.tile(z, x, y)
    if data is None:
        return Response(status_code=404)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": _CACHE_CONTROL})
