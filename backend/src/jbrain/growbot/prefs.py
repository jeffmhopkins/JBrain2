"""A persona's creature settings: the localStorage keys upstream's page keeps outside its soul
(migration 0263, docs/plans/GROWBOT_PLAN.md §2c).

The phone is only a display (owner, 2026-11-12), so what the creature chose or was told to
remember (its coat colour, its games guide, its volume, which camera it looks through, a
remembered `?flag`) lives here, per persona, and a new or wiped phone comes up as the same
creature. `growbot-box.js` decides which keys are the creature's; the box bounds their shape
and size. Writes are patches, so an owner's edit from JBrain and the creature's own change to
another key both land. A refused patch is refused, never cleaned: a phone whose write the box
rewrote would send it again forever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A key upstream writes, or one with the persona's namespace or a robot's code after it.
KEY_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./:-]{0,79}")
MAX_KEYS = 64
# The games guide is the longest value upstream keeps outside the soul, a few KB of prose.
VALUE_MAX = 16 * 1024
TOTAL_MAX = 64 * 1024
# Kept with settings the owner set before any phone sent its own: the first phone then lays its
# own under them instead of losing them. No phone key can have this name (KEY_RE).
OWNER_ONLY = "~owner"


class PrefsRefused(ValueError):
    """A write the box will not keep; the phone stops sending it until its next change."""


class PrefsTooBig(PrefsRefused):
    pass


@dataclass(frozen=True)
class Patch:
    set: dict[str, str]
    drop: list[str]


def _value_ok(value: Any) -> bool:
    # A lone surrogate or a NUL cannot be stored as jsonb.
    return (
        isinstance(value, str)
        and "\x00" not in value
        and not any("\ud800" <= c <= "\udfff" for c in value)
    )


def _key_ok(key: Any) -> bool:
    return isinstance(key, str) and KEY_RE.fullmatch(key) is not None


def patch_from(set_: Any, drop: Any) -> Patch:
    """`set` (key to new value) and `drop` (keys removed), checked before anything is merged."""
    set_ = {} if set_ is None else set_
    drop = [] if drop is None else drop
    if not isinstance(set_, dict) or not isinstance(drop, list):
        raise PrefsRefused("not a settings patch")
    if len(set_) + len(drop) > MAX_KEYS:
        raise PrefsTooBig("too many settings")
    for key, value in set_.items():
        if not _key_ok(key) or not _value_ok(value):
            raise PrefsRefused("not a setting")
        if len(value) > VALUE_MAX:
            raise PrefsTooBig("a setting too long")
    if not all(_key_ok(k) for k in drop):
        raise PrefsRefused("not a setting's name")
    return Patch(dict(set_), list(drop))


def for_phone(prefs: dict[str, Any] | None) -> tuple[dict[str, str] | None, bool]:
    """What a phone is shown: the settings without the marker, and whether no phone has sent
    its own yet (so it should keep its own under them)."""
    if prefs is None:
        return None, False
    return {k: v for k, v in prefs.items() if k != OWNER_ONLY}, OWNER_ONLY in prefs


def merged(prefs: dict[str, Any] | None, patch: Patch) -> dict[str, str]:
    """`prefs` with `patch` applied, held to the caps as a whole."""
    out = {k: v for k, v in (prefs or {}).items() if isinstance(v, str)}
    for key in patch.drop:
        out.pop(key, None)
    out.update(patch.set)
    # The owner-only mark is the box's own and never costs a setting its room.
    counted = {k: v for k, v in out.items() if k != OWNER_ONLY}
    if len(counted) > MAX_KEYS or sum(len(k) + len(v) for k, v in counted.items()) > TOTAL_MAX:
        raise PrefsTooBig("settings too big")
    return out
