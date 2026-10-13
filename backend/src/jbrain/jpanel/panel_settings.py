"""The desk panel's settings between the PWA and the glass, with no I/O (firmware-c6 0.0.21).

The owner approved a "Panel settings" section on the display panel's card on 2026-10-13
(docs/mocks/jpanel-c6/PwaPanelSettings.dc.html): the night clock, its hours and colour, the day
and date under the time, the time zone, the three notice switches, brightness, volume, read-aloud
and auto-rotate. Microphone gain and the tilt sensor's axes stay in the debug console.

THE PANEL IS THE SOURCE OF TRUTH. It keeps its settings in NVS, the owner changes them at the
glass, and it reports all of them on the debug pickup it makes every 3 s (`parse_report`). The
PWA's change is a REQUEST queued on the box (`app.endpoint_panel_setting`, migration 0245): the
pickup's answer says one waits, the panel fetches it, applies it through `svc_settings` (which
clamps, as for every writer) and its next report settles it.

THE RACE (owner, 2026-10-13: "the panel's last report wins"). Each request remembers what the
panel reported for that key when the owner made it (`base`). A report of `value` settles it; a
report still of `base` keeps it waiting (the panel has not taken it yet, or was offline); a
report of anything else means it was changed at the glass meanwhile, and the request is dropped
(`reconcile`). The panel applies a request only while it still holds `base` itself, so a change
made at the glass between the fetch and the apply is never overwritten either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jbrain.jpanel.notice import PANEL_ZONES


@dataclass(frozen=True)
class Setting:
    """One PWA-managed setting: the API's name, the pickup's short query name (the panel builds
    the URL in a fixed buffer) and the range `svc_settings_clamp` holds it to."""

    name: str
    wire: str
    lo: int
    hi: int


#: In the order the PWA groups them (Clock, Notices, Screen and sound). The ranges mirror
#: firmware-c6 `svc_settings_clamp`; the switches are 0/1.
SETTINGS: tuple[Setting, ...] = (
    Setting("night_on", "no", 0, 1),
    Setting("night_from", "nf", 0, 1439),
    Setting("night_to", "nt", 0, 1439),
    Setting("night_color", "nk", 0, 2),
    Setting("show_date", "sd", 0, 1),
    Setting("tz", "tz", 0, len(PANEL_ZONES) - 1),
    Setting("notif_accept", "na", 0, 1),
    Setting("notif_quiet", "nq", 0, 1),
    Setting("notif_chime", "nc", 0, 1),
    Setting("brightness", "br", 10, 100),
    Setting("volume", "vo", 10, 100),
    Setting("read_aloud", "ra", 0, 1),
    Setting("auto_rotate", "ar", 0, 1),
)
BY_NAME = {s.name: s for s in SETTINGS}
BY_WIRE = {s.wire: s for s in SETTINGS}

#: The six the notice switches already rode on since 0.0.19 (`notice.PREF_PARAMS`). A panel whose
#: `storage` did not mount reports none of them (it cannot show a notice); the rest still come.
NOTICE_WIRES = frozenset({"na", "nq", "nc", "nf", "nt", "tz"})
#: What a 0.0.21 panel always reports while its UI is up. Without them it is older firmware, which
#: cannot take a request: the PWA says so rather than queue one nothing will ever apply.
CORE_WIRES = frozenset(s.wire for s in SETTINGS) - NOTICE_WIRES

#: The night clock's colours, by `night_color` (firmware `clock_model.h`).
NIGHT_COLORS = ("Red", "Amber", "Grey")

#: The pickup's answer header while a request waits: the queue's version.
HEADER = "X-Panel-Settings"


class BadSetting(ValueError):
    """A request the panel could not take: an unknown key or a value outside its range."""

    def __init__(self, key: str, why: str) -> None:
        super().__init__(f"{key}: {why}")
        self.key = key
        self.why = why


def parse_report(params: dict[str, str]) -> dict[str, int] | None:
    """The panel's settings as its pickup reported them, by API name — or None when it reported
    none (firmware before 0.0.21) or any value is malformed or out of range: a half-understood
    report would settle or drop a request on a guess."""
    if not all(w in params for w in CORE_WIRES):
        return None
    out: dict[str, int] = {}
    for s in SETTINGS:
        raw = params.get(s.wire)
        if raw is None:
            continue
        try:
            v = int(raw)
        except ValueError:
            return None
        if not s.lo <= v <= s.hi:
            return None
        out[s.name] = v
    return out


def check(changes: dict[str, Any]) -> dict[str, int]:
    """The owner's request, every key known and every value in its range — refused whole
    otherwise, so a half-applied form never reaches the panel."""
    out: dict[str, int] = {}
    for key, value in changes.items():
        s = BY_NAME.get(key)
        if s is None:
            raise BadSetting(key, "not a panel setting")
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, int):
            raise BadSetting(key, "a whole number")
        if not s.lo <= value <= s.hi:
            raise BadSetting(key, f"{s.lo}-{s.hi}")
        out[key] = value
    return out


Desired = dict[str, dict[str, int]]


def request(desired: Desired, reported: dict[str, int], changes: dict[str, int]) -> Desired:
    """The queue after the owner asks for `changes`. Each remembers what the panel reports now as
    its `base`; asking for what the panel already holds withdraws any request for that key.
    `KeyError` for a key the panel has not reported (its firmware has no such setting)."""
    out = {k: dict(v) for k, v in desired.items()}
    for key, value in changes.items():
        base = reported[key]
        if value == base:
            out.pop(key, None)
        else:
            out[key] = {"value": value, "base": base}
    return out


def reconcile(desired: Desired, reported: dict[str, int]) -> tuple[Desired, list[str], list[str]]:
    """What a report does to the queue: `(still waiting, settled, dropped)`. Settled: the panel
    reports the value asked for. Dropped: it reports neither that nor the base it was asked
    over — set at the glass meanwhile, and the panel's word wins. A key it did not report waits."""
    keep: Desired = {}
    settled: list[str] = []
    dropped: list[str] = []
    for key, want in desired.items():
        got = reported.get(key)
        if got is None or got == want.get("base"):
            keep[key] = want
        elif got == want.get("value"):
            settled.append(key)
        else:
            dropped.append(key)
    return keep, settled, dropped


def wire_pending(version: int, desired: Desired) -> dict[str, Any]:
    """The panel's copy of the queue: `{"v": 7, "s": {"br": [60, 50], …}}` — each setting's
    short name, then the value and the base it was asked over, which the panel checks against
    what it holds before applying (`settings_sync.c`)."""
    return {
        "v": version,
        "s": {
            BY_NAME[k].wire: [int(w["value"]), int(w["base"])]
            for k, w in sorted(desired.items())
            if k in BY_NAME
        },
    }
