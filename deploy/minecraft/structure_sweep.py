"""The structure index behind the map's Structures overlay (MINECRAFT_BEDROCK_PLAN §M8).

Bedrock places structures differently from Java, so cubiomes can't predict them; the
running server's own `locate structure` is exact, read-only and answers nearest-from-a-
point. A sweep asks it from a grid of points over everywhere anyone has explored (plus
spawn) and a margin beyond, and keeps every distinct answer.

The result is saved per world folder in the sidecar's data volume, so the overlay draws
while the server is stopped and the sweep runs once per world, not per view. Structures
are fixed by the seed, so a saved sweep only goes stale when the seed changes (a reset or
a restore of a different world), when the explored area grows past what was swept, or
when a server update teaches `locate` a kind the sweep didn't ask about. It is kept
outside the world folder so a `.mcworld` export carries nothing the game didn't write.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Points this far apart: close enough that most structures are some point's nearest of
# their kind, few enough that a world's sweep is a few minutes of console time.
STEP = 512
# How far past the explored edge to look: "the village just over that hill".
MARGIN = 1024
# No sweep reaches further than this from the explored area's centre, whatever its size.
REACH = 4096
# Growth past the swept area smaller than this isn't worth another sweep.
SLACK = STEP // 2
FORMAT = 1

Extent = dict[str, int]
Locate = Callable[[str, int, int, str], tuple[int, int] | None]


def axis(lo: int, hi: int, step: int = STEP) -> list[int]:
    """Evenly spaced whole coordinates from lo to hi, no further apart than `step`."""
    if hi <= lo:
        return [lo]
    n = math.ceil((hi - lo) / step)
    return [round(lo + (hi - lo) * i / n) for i in range(n + 1)]


def area(extent: Extent | None, spawn: tuple[int, int]) -> Extent:
    """The block rectangle a sweep covers: the explored extent (or spawn, in a dimension
    nobody has visited) plus the margin, capped at REACH around its centre."""
    if extent is None:
        x0 = x1 = spawn[0]
        z0 = z1 = spawn[1]
    else:
        x0, x1 = min(extent["min_x"], spawn[0]), max(extent["max_x"], spawn[0])
        z0, z1 = min(extent["min_z"], spawn[1]), max(extent["max_z"], spawn[1])
    cx, cz = (x0 + x1) // 2, (z0 + z1) // 2
    return {
        "min_x": max(x0 - MARGIN, cx - REACH),
        "max_x": min(x1 + MARGIN, cx + REACH),
        "min_z": max(z0 - MARGIN, cz - REACH),
        "max_z": min(z1 + MARGIN, cz + REACH),
    }


def points(rect: Extent, spawn: tuple[int, int]) -> list[tuple[int, int]]:
    """The grid over `rect`, spawn first and then outward from the middle, so a sweep
    cut short still has what is nearest."""
    cx, cz = (rect["min_x"] + rect["max_x"]) / 2, (rect["min_z"] + rect["max_z"]) / 2
    grid = [
        (x, z)
        for x in axis(rect["min_x"], rect["max_x"])
        for z in axis(rect["min_z"], rect["max_z"])
    ]
    grid.sort(key=lambda p: math.hypot(p[0] - cx, p[1] - cz))
    return [spawn, *(p for p in grid if p != spawn)]


def stale_reason(
    saved: dict[str, Any] | None,
    seed: str | None,
    kinds: dict[str, tuple[str, ...]],
    extents: dict[str, Extent | None],
) -> str | None:
    """Why a saved sweep no longer answers for this world, or None while it does."""
    if not saved:
        return "never searched"
    if saved.get("seed") != seed:
        return "the world changed"
    for dim, wanted in kinds.items():
        if set(saved.get("kinds", {}).get(dim, ())) != set(wanted):
            return "the server knows new structure kinds"
        ext, swept = extents.get(dim), saved.get("explored", {}).get(dim)
        if ext is None:
            continue
        if swept is None:
            return "explored further"
        if (
            ext["min_x"] < swept["min_x"] - SLACK
            or ext["max_x"] > swept["max_x"] + SLACK
            or ext["min_z"] < swept["min_z"] - SLACK
            or ext["max_z"] > swept["max_z"] + SLACK
        ):
            return "explored further"
    return None


def sweep(
    locate: Locate,
    plan: dict[str, list[tuple[int, int]]],
    kinds: dict[str, tuple[str, ...]],
    progress: Callable[[int], None] = lambda _done: None,
    keep_going: Callable[[], bool] = lambda: True,
) -> dict[str, list[dict[str, Any]]]:
    """Ask `locate` for every kind from every point and keep each structure once, nearest
    to the world's centre first. Raises whatever `locate` raises; stops early (raising
    InterruptedError) when `keep_going` says the world has gone."""
    found: dict[str, dict[tuple[str, int, int], dict[str, Any]]] = {d: {} for d in plan}
    done = 0
    for dim, pts in plan.items():
        for x, z in pts:
            for kind in kinds[dim]:
                if not keep_going():
                    raise InterruptedError("the world was unloaded mid-search")
                got = locate(dim, x, z, kind)
                done += 1
                progress(done)
                if got is not None:
                    found[dim].setdefault((kind, got[0], got[1]), {
                        "structure": kind,
                        "x": got[0],
                        "z": got[1],
                    })
    return {
        dim: sorted(hits.values(), key=lambda s: (abs(s["x"]) + abs(s["z"]), s["structure"]))
        for dim, hits in found.items()
    }


def total(plan: dict[str, list[tuple[int, int]]], kinds: dict[str, tuple[str, ...]]) -> int:
    return sum(len(pts) * len(kinds[dim]) for dim, pts in plan.items())


def path_for(root: Path, folder: str) -> Path:
    return root / f"{folder}.json"


def load(root: Path, folder: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(path_for(root, folder).read_text())
    except (FileNotFoundError, ValueError):
        return None
    return raw if isinstance(raw, dict) and raw.get("format") == FORMAT else None


def save(
    root: Path,
    folder: str,
    *,
    seed: str | None,
    kinds: dict[str, tuple[str, ...]],
    explored: dict[str, Extent | None],
    found: dict[str, list[dict[str, Any]]],
    at: float,
) -> dict[str, Any]:
    """Write the sweep atomically: a reader never sees half a file."""
    body = {
        "format": FORMAT,
        "folder": folder,
        "seed": seed,
        "swept_at": at,
        "kinds": {d: list(k) for d, k in kinds.items()},
        "explored": explored,
        "structures": found,
    }
    root.mkdir(parents=True, exist_ok=True)
    tmp = path_for(root, folder).with_suffix(".tmp")
    tmp.write_text(json.dumps(body))
    os.replace(tmp, path_for(root, folder))
    return body

