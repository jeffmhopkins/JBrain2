"""Ground the players have been to, for the map's tiles (MINECRAFT_BEDROCK_PLAN §M8).

Bedrock saves a chunk only once something in it changes, a mob spawning or a block
placed: ground a player only walked or flew over is generated, shown to them, then
dropped, because the seed regenerates it exactly. So the world's files alone leave holes
along every trail (checked on 1.26.52.3: a server's ticking areas loaded about 50 chunks
and saved 9). The travel log's fog of war (`app.mc_player_explored`) knows where players
went, so each tile request carries that ground to the sidecar, which draws the seed's
prediction there at full strength rather than as the dimmed survey.

The chunks go as a bitmap, one bit per chunk of the tile, zlib-compressed and base64url:
a zoomed-out tile spans 256 × 256 chunks, and a trail's ground compresses to a few
hundred bytes.
"""

from __future__ import annotations

import base64
import time
import zlib
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.minecraft import client as mc

_OWNER = SessionContext(principal_kind="owner")
CHUNKS_PER_TILE = 16  # a zoom-0 tile is 256 blocks
# A slot's folder changes only when a world is created into it or imported, and a tile
# view asks for dozens of tiles at once: one /worlds read serves them all.
FOLDER_TTL_S = 30.0
_folders: dict[str, tuple[float, str]] = {}

VISITED_SQL = text(
    """
    SELECT DISTINCT cx, cz
      FROM app.mc_player_explored
     WHERE world = :world AND dim = :dim
       AND cx >= :cx0 AND cx < :cx1 AND cz >= :cz0 AND cz < :cz1
    """
)


def tile_span(zoom: int) -> int:
    """How many chunks a tile at this zoom spans, each way."""
    return CHUNKS_PER_TILE << zoom


def encode(chunks: set[tuple[int, int]], cx0: int, cz0: int, span: int) -> str:
    """The bitmap of `chunks` inside the span×span square at (cx0, cz0): bit
    `(cz - cz0) * span + (cx - cx0)`, least significant first in each byte."""
    bits = bytearray(span * span // 8)
    for cx, cz in chunks:
        i = (cz - cz0) * span + (cx - cx0)
        bits[i >> 3] |= 1 << (i & 7)
    return base64.urlsafe_b64encode(zlib.compress(bytes(bits), 9)).decode().rstrip("=")


async def folder_of(settings: Any, slot: str, clock=time.monotonic) -> str:
    now = clock()
    hit = _folders.get(slot)
    if hit is not None and hit[0] > now:
        return hit[1]
    folder = str((await mc.slot_record(settings, slot))["folder"])
    _folders[slot] = (now + FOLDER_TTL_S, folder)
    return folder


async def tile_params(
    maker: async_sessionmaker[AsyncSession],
    folder: str,
    dim: str,
    zoom: int,
    tx: int,
    tz: int,
    ctx: SessionContext = _OWNER,
) -> dict[str, str]:
    """The `visited` parameter for one tile's request, or nothing when no player has
    been in it. A map share reads the fog under its own pinned `ctx`."""
    span = tile_span(zoom)
    cx0, cz0 = tx * span, tz * span
    bounds = {"cx0": cx0, "cx1": cx0 + span, "cz0": cz0, "cz1": cz0 + span}
    async with scoped_session(maker, ctx) as s:
        rows = (await s.execute(VISITED_SQL, {"world": folder, "dim": dim, **bounds})).all()
    if not rows:
        return {}
    return {"visited": encode({(int(r[0]), int(r[1])) for r in rows}, cx0, cz0, span)}


async def for_slot(
    maker: async_sessionmaker[AsyncSession],
    settings: Any,
    slot: str,
    dim: str,
    zoom: int,
    tx: int,
    tz: int,
) -> dict[str, str]:
    """`tile_params` for the owner's tile of a slot. A slot with no world has no ground
    to add: the sidecar's own answer for its tile stands."""
    try:
        folder = await folder_of(settings, slot)
    except HTTPException as exc:
        if exc.status_code == 404:
            return {}
        raise
    return await tile_params(maker, folder, dim, zoom, tx, tz)
