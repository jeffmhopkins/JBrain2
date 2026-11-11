"""A persona's visual memories: the saved views upstream's `v3/visual-memory.js` keeps of the
people and things the creature has met (plan §2c, wave G4d).

A phone's record is checked here before the box keeps it, the way an imported soul is
(plan: "visual-memory blobs are type- and size-checked"). It is refused, never cleaned: the
phone syncs by comparing what it holds with what the box holds field for field, so a record the
box rewrote would never match and would be sent again forever.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
from dataclasses import dataclass
from typing import Any

# visual-memory.js: VM_KINDS, plus "thing", an older name for "object" it still reads.
KINDS = frozenset({"person", "object", "handwriting", "scene", "place", "thing"})
ID_RE = re.compile(r"r[0-9a-f]{1,32}")
# vmName and vmDescription cap at 60 and 200 UTF-16 units; a code point is never more.
NAME_MAX = 60
DESC_MAX = 200
MAX_VIEWS = 3
# visual-memory.js VM_MAX_B64: the base64 of one view.
MAX_VIEW_B64 = 800_000
# Upstream has no cap on how many it keeps; at three views each this bounds a persona's disk.
MAX_MEMORIES = 500
JPEG = b"\xff\xd8\xff"


class MemoryRefused(ValueError):
    """A record the box will not keep; the phone stops sending it."""


@dataclass(frozen=True)
class Saved:
    """A record ready to keep: its id, the creature's fields, and each view's JPEG and time."""

    id: str
    meta: dict[str, Any]
    views: list[tuple[bytes, float | int]]


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _text(value: Any, cap: int) -> bool:
    # A lone surrogate (upstream's slice can cut an emoji in half) or a NUL cannot be stored
    # as jsonb: refused, so the phone stops sending it, instead of failing on every retry.
    return (
        isinstance(value, str)
        and len(value) <= cap
        and "\x00" not in value
        and not any("\ud800" <= c <= "\udfff" for c in value)
    )


def from_phone(raw: Any) -> Saved:
    if not isinstance(raw, dict):
        raise MemoryRefused("not a visual memory")
    mid = raw.get("id")
    if not isinstance(mid, str) or not ID_RE.fullmatch(mid):
        raise MemoryRefused("not a visual memory id")
    if raw.get("kind") not in KINDS:
        raise MemoryRefused("not a kind of visual memory")
    if not _text(raw.get("name"), NAME_MAX) or not _text(raw.get("desc"), DESC_MAX):
        raise MemoryRefused("a name or description too long")
    if raw.get("confirmed") not in ("human", "agent"):
        raise MemoryRefused("not who confirmed it")
    if not _number(raw.get("t")) or not _number(raw.get("namedAt")):
        raise MemoryRefused("not when it was saved")
    views = raw.get("views")
    if not isinstance(views, list) or not 1 <= len(views) <= MAX_VIEWS:
        raise MemoryRefused("one to three views")
    out: list[tuple[bytes, float | int]] = []
    for view in views:
        j = view.get("j") if isinstance(view, dict) else None
        if not isinstance(j, str) or not j or len(j) > MAX_VIEW_B64 or not _number(view.get("t")):
            raise MemoryRefused("a view that is not a picture")
        try:
            data = base64.b64decode(j, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MemoryRefused("a view that is not a picture") from exc
        if not data.startswith(JPEG):
            raise MemoryRefused("a view that is not a JPEG")
        out.append((data, view["t"]))
    meta = {k: raw[k] for k in ("kind", "name", "desc", "t", "confirmed", "namedAt")}
    return Saved(id=mid, meta=meta, views=out)
