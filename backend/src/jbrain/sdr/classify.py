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

# The two identifiers that are legitimately control bytes. Every OTHER byte below space is
# a crafted or corrupt frame, and one of them is unstorable: Postgres rejects a NUL in a
# text column, the insert path swallows its own errors so the log keeps running, and the
# row — with the `raw` that would have let us re-derive it — is gone. A frame whose info
# field simply begins with 0x00 is a perfectly forwardable AX.25 UI frame, so this is
# reachable from the air by anyone with a transmitter.
_MIC_E_CONTROL = frozenset({"\x1c", "\x1d"})

# What an unwrapped origin may look like. Deliberately LOOSER than AX.25's own address
# rules, because a third-party header is plain text from APRS-IS and legitimately carries
# things AX.25 cannot — alphanumeric SSIDs (`K4JTT-D`, `N1MPR-C`) and service names longer
# than six characters (`WINLINK`) are all in the owner's own capture. What it rejects is
# what a station cannot be: spaces, punctuation and markup, and in particular the `*` that
# would otherwise let one crafted frame mint `N4TDX*` beside the real `N4TDX` in a roster.
_ORIGIN_MAX = 9
_SSID_MAX = 2

# How deep to unwrap. Nesting is legal and a crafted frame could nest indefinitely.
_MAX_DEPTH = 2

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


def _storable_dti(char: str) -> str:
    """A data-type identifier we are willing to put in a text column, or "".

    The two Mic-E identifiers below space survive; nothing else does. See `_MIC_E_CONTROL`
    — this is the guard that stops one crafted byte deleting a row that `raw` would
    otherwise have made recoverable forever."""
    return char if char >= " " or char in _MIC_E_CONTROL else ""


def _norm(call: str) -> str:
    """A callsign as we file it: trimmed, upper case, bounded."""
    return call.strip().upper()[:16]


def looks_like_station(call: str) -> bool:
    """Whether an unwrapped origin is a station rather than crafted text.

    The third-party header is attacker-authored — it is plain text inside `info`, not an
    AX.25 address the sidecar validated — and F2's roster is one row per origin. Without
    this, one transmitter emitting a hundred invented origins puts a hundred stations on
    the owner's screen, including ones that shadow real callsigns."""
    base, dash, ssid = call.partition("-")
    if not call or len(call) > _ORIGIN_MAX or not base.isascii() or not base.isalnum():
        return False
    return not dash or (0 < len(ssid) <= _SSID_MAX and ssid.isascii() and ssid.isalnum())


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
        return _storable_dti(info[:1])
    # AX.25 UI: addresses (7 bytes each, last flagged by bit 0 of its final byte), then
    # control and PID, then the info field.
    i = 0
    while i + 7 <= len(frame):
        last = frame[i + 6] & 0x01
        i += 7
        if last:
            break
    else:
        return _storable_dti(info[:1])
    if i + 2 >= len(frame):
        return _storable_dti(info[:1])
    return _storable_dti(chr(frame[i + 2])) or _storable_dti(info[:1])


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


# How many characters of timestamp sit between the identifier and the position, by
# identifier. Only these four carry a position in the fixed layouts below.
_TIMESTAMP_LEN = {"!": 0, "=": 0, "/": 7, "@": 7}

# Where the symbol code sits, measured from where the position starts. APRS101 defines
# TWO layouts and they are not the same length: uncompressed is 8 of latitude, the symbol
# TABLE, 9 of longitude, then the CODE; compressed (ch.9) is the table, 4 of latitude, 4
# of longitude, then the CODE.
_UNCOMPRESSED_CODE_AT = 18
_COMPRESSED_CODE_AT = 9


def _symbol_code(dti: str, info: str) -> str:
    """The symbol code of a position report, or "" if it has none.

    Which layout is in use is told by the first byte of the position: an uncompressed
    report starts its latitude with a digit (or a space, for an ambiguous fix), a
    compressed one starts with its symbol TABLE — `/`, `\\`, or an overlay `A`-`Z`/`a`-`j`.

    Reading the exact offset rather than scanning for the character is the point: an
    underscore in a station's comment would otherwise turn every one of its positions into
    a weather report. Reading it at the RIGHT offset for both layouts is why this is not
    just a lookup table — a compressed weather station is invisible to one that assumes
    the uncompressed layout, and the measured capture has compressed traffic on it."""
    stamp = _TIMESTAMP_LEN.get(dti)
    if stamp is None:
        return ""
    start = 1 + stamp
    if len(info) <= start:
        return ""
    head = info[start]
    if "0" <= head <= "9" or head == " ":
        at = start + _UNCOMPRESSED_CODE_AT
    elif head in "/\\" or "A" <= head <= "Z" or "a" <= head <= "j":
        at = start + _COMPRESSED_CODE_AT
    else:
        return ""
    return info[at] if len(info) > at else ""


def _is_weather_position(dti: str, info: str) -> bool:
    """A position report whose symbol code is `_` is a weather report wearing a position
    identifier (APRS101 ch.12). Without this the Weather filter misses the actual weather
    stations, which on the measured capture is most of them."""
    return _symbol_code(dti, info) == "_"


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
    # Nesting past the cap leaves a wrapper we never opened. Its `}` is a transport, never
    # a type, so refusing to read an identifier off it is more honest than storing one.
    over_depth = effective.startswith(THIRD_PARTY)
    # Third-party alone does NOT mean internet: an RF-to-RF cross-band relay, a satellite
    # downlink and a mesh bridge all wrap frames too, and those genuinely came off the
    # air. TCPIP/TCPXX in the inner path is what says otherwise.
    gated = wrapped and any(e.strip("*").upper() in {"TCPIP", "TCPXX"} for e in eff_path)
    if over_depth:
        dti = ""
    elif wrapped:
        dti = _storable_dti(effective[:1])
    else:
        dti = dti_from_raw(raw_hex, effective)
    # An origin that is not shaped like a station is not filed as one: the frame is
    # attributed to the station that actually transmitted it, which is the only callsign
    # here the sidecar validated.
    named = _norm(origin)
    return Heard(
        origin=named if looks_like_station(named) else _norm(source),
        relay=relay,
        dti=dti,
        kind=_kind(dti, effective),
        gated=gated,
        # A `*` marks the digipeater that repeated it, and the path that says whether WE
        # heard the sender is the OUTER one — the inner path of a relayed frame describes
        # somebody else's hop, and is attacker-authored text besides. A wrapped frame is
        # never "the sender's own transmission as we received it", whatever it claims.
        direct=not wrapped and not any("*" in e for e in path),
        addressee=_addressee(effective),
        text=effective,
    )


def base_call(call: str) -> str:
    """A callsign without its SSID. `N0CALL-9` from the truck and `N0CALL-7` from the
    handheld are one operator, and an owner who typed the bare call meant both."""
    return (call or "").strip().upper().split("-", 1)[0]
