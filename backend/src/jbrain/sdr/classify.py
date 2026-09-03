"""What a heard frame actually IS — the derivation the whole APRS log leans on.

A packet channel does not look the way the AX.25 header says it does. Measured on the
owner's box over 90 minutes on 144.390: 184 frames, **5 values in the source column, 15
real senders**. Three quarters of the log was one IGate relaying internet traffic onto RF
as third-party frames, and for every one of those the AX.25 source names the RELAY. So
"filter by station" built on `source` is wrong for most of the log, and "filter by type"
built on the first character of `info` is wrong for exactly the same rows.

This module is the fix, and it is deliberately pure: bytes in, a struct out, no I/O. The
derived values are a CACHE over `raw`, which is stored losslessly, so a better classifier
can always be backfilled — nothing here has to be right the first time to be safe.

**Everything it reads is hostile.** These are transmissions from anyone with a radio, and
a callsign is plain bytes that forge trivially. The output narrows and labels; it never
authenticates, and nothing may gate an action on it.
"""

from __future__ import annotations

from dataclasses import dataclass

# The five buckets the GUI filters on (docs/mocks/aprs/e-stations.html). APRS defines
# around twenty-five data-type identifiers; a phone filter with fifteen equal chips is
# worse than one with five that matter, and both YAAC and the APRS-IS `t/` filter collapse
# to about this granularity from different directions.
#
# Mic-E folds into Position on purpose: a ham does not care that a D710 encodes its
# position differently, only that it is a position.
POSITION = "Position"
MESSAGE = "Message"
WEATHER = "Weather"
OBJECT = "Object"
OTHER = "Other"

_KIND_BY_DTI = {
    "!": POSITION,  # position, no timestamp — or an Ultimeter 2000 weather station
    "=": POSITION,  # position, no timestamp, messaging capable
    "/": POSITION,  # position with timestamp
    "@": POSITION,  # position with timestamp, messaging capable
    "`": POSITION,  # current Mic-E
    "'": POSITION,  # old Mic-E (but CURRENT for a TM-D700)
    "\x1c": POSITION,  # current Mic-E, Rev 0 beta — a control byte, see `dti_from_raw`
    "\x1d": POSITION,  # old Mic-E, Rev 0 beta
    "$": POSITION,  # raw NMEA
    ":": MESSAGE,
    "_": WEATHER,  # weather report without position
    "#": WEATHER,  # Peet Bros U-II
    "*": WEATHER,  # Peet Bros U-II
    ";": OBJECT,
    ")": OBJECT,  # item — the object/item split is an authorship detail, not a user one
}

# The wrapper. Never a kind of its own: it is a transport, and the packet inside it has
# its own type and its own sender.
THIRD_PARTY = "}"

# How deep to unwrap. Nesting is legal and a crafted frame could nest indefinitely.
_MAX_DEPTH = 2

# Path elements that are DIRECTIVES, not stations. They occupy a path slot and must never
# reach a "digipeated by" list or a station roster.
_PATH_FLAGS = frozenset({"TCPIP", "TCPXX", "NOGATE", "RFONLY"})

# A message addressee is a fixed nine characters, space padded, then a colon.
_ADDRESSEE_WIDTH = 9

# An APRS position report may carry up to 40 characters of unmodifiable leading text
# before its `!`, to accommodate X1J digipeaters (APRS101 ch.5). A frame that looks like
# an unknown type but has a `!` inside that window is a position report.
_DEFERRED_POSITION_WINDOW = 40


@dataclass(frozen=True, slots=True)
class Heard:
    """What one frame turned out to be, once the wrappers are off."""

    origin: str
    """The true sender. For a third-party frame this is the station INSIDE the wrapper,
    not the relay that put it on the air."""

    relay: str | None
    """The station that transmitted it, when that differs from the origin."""

    dti: str
    """The effective APRS data-type identifier — the inner one for a wrapped frame."""

    kind: str
    """One of the five buckets above."""

    gated: bool
    """Third-party AND carrying TCPIP/TCPXX: it came from the internet rather than off
    the air. NOT the same as third-party — an RF-to-RF or satellite relay is also
    wrapped, and that frame genuinely was heard."""

    direct: bool
    """No digipeater marked itself in the path, so this is the sender's own transmission
    as we received it. The fact a ham most wants per packet."""

    addressee: str | None
    """For a message, who it is addressed to, trimmed. None for everything else."""

    text: str
    """The effective info field — the inner one for a wrapped frame."""


def dti_from_raw(raw_hex: str, info: str) -> str:
    """The data-type identifier, taken from the untouched frame when possible.

    `info` has been through a control-character scrub on the way to the database, and two
    legitimate Mic-E identifiers (0x1C, 0x1D) ARE control characters — so for those frames
    the byte that says what the packet is has been deleted from `info`. `raw` is stored
    before any of that, which is what makes it recoverable.

    Falls back to `info` when `raw` is unusable: a missing identifier is a classification
    problem, never a reason to lose the row."""
    try:
        frame = bytes.fromhex(raw_hex)
    except ValueError:
        return info[:1]
    # AX.25 UI: addresses (7 bytes each, last flagged by bit 0 of its final byte), then
    # control and PID, then the info field.
    i = 0
    while i + 7 <= len(frame):
        last = frame[i + 6] & 0x01
        i += 7
        if last:
            break
    else:
        return info[:1]
    if i + 2 >= len(frame):
        return info[:1]
    return chr(frame[i + 2])


def _split_third_party(info: str) -> tuple[str, list[str], str] | None:
    """`}SRC>DEST,path:payload` → (origin, path, payload), or None if it is not one.

    The header cannot contain a colon — it is callsigns, `>`, `,` and `*` — so the first
    colon ends it unambiguously."""
    if not info.startswith(THIRD_PARTY):
        return None
    body = info[1:]
    colon = body.find(":")
    if colon < 0:
        return None
    head, payload = body[:colon], body[colon + 1 :]
    arrow = head.find(">")
    if arrow < 0:
        return None
    rest = head[arrow + 1 :].split(",")
    return head[:arrow], rest[1:], payload


def _addressee(info: str) -> str | None:
    """Who a message is for, or None.

    Nine fixed characters then a colon — NOT a split on `:`, because message text legally
    contains colons (times, URLs, ratios) and a split-based parser breaks on the first
    one it meets."""
    if not info.startswith(":") or len(info) < _ADDRESSEE_WIDTH + 2:
        return None
    if info[1 + _ADDRESSEE_WIDTH] != ":":
        return None
    return info[1 : 1 + _ADDRESSEE_WIDTH].strip() or None


# Where the symbol code sits in an uncompressed position report, by identifier. The
# layout is fixed: identifier, then (for the timestamped forms) seven characters of
# timestamp, then 8 of latitude, the symbol TABLE, 9 of longitude, and the symbol CODE.
# Reading the exact offset rather than scanning for the character matters — an
# underscore in a station's comment would otherwise turn every one of its positions
# into a weather report.
_SYMBOL_CODE_AT = {"!": 19, "=": 19, "/": 26, "@": 26}


def _is_weather_position(dti: str, info: str) -> bool:
    """A position report whose symbol code is `_` is a weather report wearing a position
    identifier (APRS101 ch.12). Without this the Weather filter misses the actual weather
    stations, which on the measured capture is most of them."""
    at = _SYMBOL_CODE_AT.get(dti)
    return at is not None and len(info) > at and info[at] == "_"


def _kind(dti: str, info: str) -> str:
    kind = _KIND_BY_DTI.get(dti)
    if kind is not None:
        if kind is POSITION and _is_weather_position(dti, info):
            return WEATHER
        return kind
    # The deferred `!`: leading unmodifiable text before the real identifier.
    if "!" in info[:_DEFERRED_POSITION_WINDOW]:
        return POSITION
    return OTHER


def classify(source: str, info: str, path: list[str], raw_hex: str = "") -> Heard:
    """Derive what a stored frame is. Total: every input yields a Heard."""
    origin, relay, effective, eff_path = source, None, info, list(path)
    for _ in range(_MAX_DEPTH):
        inner = _split_third_party(effective)
        if inner is None:
            break
        relay = relay or source
        origin, eff_path, effective = inner[0], inner[1], inner[2]

    wrapped = relay is not None
    # Third-party alone does NOT mean internet: an RF-to-RF cross-band relay, a satellite
    # downlink and a mesh bridge all wrap frames too, and those genuinely came off the
    # air. TCPIP/TCPXX in the inner path is what says otherwise.
    gated = wrapped and any(e.strip("*").upper() in {"TCPIP", "TCPXX"} for e in eff_path)
    dti = dti_from_raw(raw_hex, effective) if not wrapped else effective[:1]
    return Heard(
        origin=origin.strip().upper()[:16] or source,
        relay=relay,
        dti=dti,
        kind=_kind(dti, effective),
        gated=gated,
        # A `*` marks the digipeater that repeated it. None anywhere means we heard the
        # sender's own transmission. A gated frame was never "direct" in any useful sense.
        direct=not gated and not any("*" in e for e in eff_path),
        addressee=_addressee(effective),
        text=effective,
    )


def base_call(call: str) -> str:
    """A callsign without its SSID. `N0CALL-9` from the truck and `N0CALL-7` from the
    handheld are one operator, and an owner who typed the bare call meant both."""
    return (call or "").strip().upper().split("-", 1)[0]


def is_path_flag(element: str) -> bool:
    """Whether a path element is a directive rather than a station."""
    return element.strip("*").upper() in _PATH_FLAGS
