"""The crossed-ground bitmap a map tile request carries (plan §M8). Its DB read is in
test_mc_visited_pg; here, the encoding the sidecar decodes, the slot→folder cache, and
what a slot with no world sends."""

from __future__ import annotations

import base64
import zlib
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.minecraft import client as mc
from jbrain.minecraft import visited


def _bits(blob: str) -> bytes:
    return zlib.decompress(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))


def test_the_bitmap_is_row_major_from_the_tiles_corner_low_bit_first() -> None:
    bits = _bits(visited.encode({(-16, 32), (-15, 32), (-16, 33), (-1, 47)}, -16, 32, 16))
    assert len(bits) == 32
    assert bits[0] == 0b11  # (0, 0) and (1, 0)
    assert bits[2] == 0b1  # (0, 1): the next row starts 16 bits on
    assert bits[31] == 0b1000_0000  # (15, 15), the last bit
    assert sum(bin(b).count("1") for b in bits) == 4
    assert visited.tile_span(0) == 16 and visited.tile_span(4) == 256


async def test_a_slots_folder_is_asked_once_per_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    async def record(_settings: Any, slot: str) -> dict[str, Any]:
        asked.append(slot)
        return {"id": slot, "folder": f"{slot}-folder", "exists": True}

    monkeypatch.setattr(mc, "slot_record", record)
    monkeypatch.setattr(visited, "_folders", {})
    now = [100.0]
    assert await visited.folder_of(None, "slot3", lambda: now[0]) == "slot3-folder"
    assert await visited.folder_of(None, "slot3", lambda: now[0]) == "slot3-folder"
    assert asked == ["slot3"]
    now[0] += visited.FOLDER_TTL_S + 1
    await visited.folder_of(None, "slot3", lambda: now[0])
    assert asked == ["slot3", "slot3"]


async def test_a_slot_with_no_world_sends_no_ground_and_other_failures_stand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visited, "_folders", {})
    status = [404]

    async def refuse(_settings: Any, slot: str) -> dict[str, Any]:
        raise HTTPException(status_code=status[0], detail=f"{slot} has no world yet")

    monkeypatch.setattr(mc, "slot_record", refuse)
    assert await visited.for_slot(None, None, "slot2", "overworld", 0, 0, 0) == {}  # type: ignore[arg-type]
    status[0] = 503  # the sidecar down: the tile fails as it would have anyway
    with pytest.raises(HTTPException):
        await visited.for_slot(None, None, "slot2", "overworld", 0, 0, 0)  # type: ignore[arg-type]
