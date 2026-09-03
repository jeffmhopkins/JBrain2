"""What a heard frame SAYS, in words — the layer between a decoded packet and a card.

`classify.py` answers "what kind of thing is this and who sent it". This answers "what
does it actually say": 78 °F with the wind from the north-north-west, a car doing 60 mph
on a heading of 242°, a 14.25 V supply that has digipeated 110 packets in the last hour.

**Why this is worth doing.** On the owner's own channel the resting list was
`` `m3jq6F>/`On D-Star ``, `T#110,190,088` and `@031030z2837.27N/08049.42W_338/000g000` —
three lines of which none can be read. The binding spec (docs/mocks/aprs/i-packet-readable
.html) puts the meaning on the row and the bytes one tap below, so this module is what the
row is made of.

**It reads `payload`, not `text`.** Two Mic-E course bytes are legitimately control
characters — 0x1C and 0x7F both occur here — and the scrub on the way to the database
deletes them, shifting every byte after. See `classify.Heard.payload`.

**Nothing here is authentication and nothing here is a fact.** Every byte came off the air
from anyone with a transmitter: a callsign forges trivially, a telemetry definition is an
ordinary message anyone can send, and a station's own timestamp is whatever its clock
says. The output is labelled *claims*, and the caller renders them as such.

**Total by construction.** Slices rather than indices, bounded loops, anchored patterns,
and a decoder that cannot read something returns what it has plus a note — never an
exception, because one crafted frame must not end the drain, and never a guess, because a
plausible wrong sentence is worse than an admitted gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jbrain.sdr import symbols
from jbrain.sdr.classify import MESSAGE, OBJECT, POSITION, WEATHER, Heard

# Mic-E message bits A/B/C, read as one 3-bit number (APRS101 ch.10 p.45). All three zero
# is an EMERGENCY — display it, never act on it: it is one crafted frame away from anyone.
_MICE_STANDARD = {
    7: "Off duty",
    6: "En route",
    5: "In service",
    4: "Returning",
    3: "Committed",
    2: "Special",
    1: "Priority",
    0: "EMERGENCY",
}

# The trailing two bytes of a Mic-E status text identify the radio (aprs-deviceid's
# `mice` table). They are device identification, not the operator's words, so they come
# off the comment rather than being shown as something a person wrote.
_MICE_DEVICES = {
    "_%": "Yaesu FTM-400DR",
    "_ ": "Yaesu VX-8",
    '_"': "Yaesu FTM-350",
    "_#": "Yaesu VX-8G",
    "_$": "Yaesu FT1D",
    "_(": "Yaesu FT2D",
    "_)": "Yaesu FTM-100D",
    "_0": "Yaesu FT3D",
    "_1": "Yaesu FTM-300D",
    "_2": "Yaesu FTM-200D",
    "_3": "Yaesu FT5D",
    "_4": "Yaesu FTM-500D",
    "]=": "Kenwood TM-D710",
    ">=": "Kenwood TH-D72",
    ">^": "Kenwood TH-D74",
    ">&": "Kenwood TH-D75",
    "(8": "Anytone D878UV",
    "(5": "Anytone D578UV",
    "|3": "Byonics TinyTrak3",
    "|4": "Byonics TinyTrak4",
    "[1": "APRSdroid",
}

_COMPASS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)

# How many characters of timestamp sit between a position identifier and the position.
_TIMESTAMP_LEN = {"!": 0, "=": 0, "/": 7, "@": 7}
# Where the symbol code sits, from where the position starts. Two layouts, and they are
# not the same length (APRS101 ch.9 vs ch.6).
_UNCOMPRESSED_CODE_AT = 18
_COMPRESSED_CODE_AT = 9

# An analogue telemetry value is 000-255 by the spec. DireWolf emits `53.2` and `-0.8`, so
# the parser accepts a bounded signed decimal and keeps the raw string when it cannot.
_TELEMETRY_VALUE = re.compile(r"^-?\d{1,3}(\.\d{1,2})?$")


@dataclass(frozen=True, slots=True)
class Field:
    """One labelled reading, and the bytes it came from.

    `raw` travels with every field so a reader can always check the sentence against what
    was actually sent — the whole design leans on the evidence being one tap away."""

    name: str
    value: str
    raw: str = ""


@dataclass(frozen=True, slots=True)
class Explained:
    """What one frame says. Never raises; an unreadable frame is an empty one plus a note."""

    summary: str
    """One line, in the app's own words. Empty when there is nothing to say that the
    station's own text does not already say better — a status report is its own summary,
    and repeating it in our voice would be reciting a stranger's sentence as ours."""

    fields: list[Field] = field(default_factory=list)
    comment: str = ""
    """The station's own free text, verbatim. Rendered as theirs, never as ours."""

    symbol: str = ""
    """The two symbol characters as transmitted, for the icon. Empty when the frame
    carries none."""

    warnings: list[str] = field(default_factory=list)
    """What could not be read, or what a reader must not assume. Shown, not swallowed."""


def _compass(deg: float) -> str:
    return _COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def _trim(value: float, places: int = 1) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def _position(body: str) -> dict[str, object] | None:
    """An uncompressed position, or None. Ambiguity — trailing spaces in the minutes —
    is preserved rather than zero-filled, because "to the nearest minute" and "to the
    nearest hundredth" are different claims about where a station is."""
    m = re.match(
        r"^(\d{2})([0-9 ]{2}\.[0-9 ]{2})([NS])(.)(\d{3})([0-9 ]{2}\.[0-9 ]{2})([EW])(.)", body
    )
    if not m:
        return None
    try:
        lat = int(m.group(1)) + float(m.group(2).replace(" ", "0")) / 60
        lon = int(m.group(5)) + float(m.group(6).replace(" ", "0")) / 60
    except ValueError:
        return None
    if m.group(3) == "S":
        lat = -lat
    if m.group(7) == "W":
        lon = -lon
    return {
        "lat": lat,
        "lon": lon,
        "table": m.group(4),
        "code": m.group(8),
        "ambiguity": m.group(2).count(" "),
        "rest": body[m.end() :],
    }


def _place(pos: dict[str, object]) -> str:
    lat, lon = float(pos["lat"]), float(pos["lon"])  # type: ignore[arg-type]
    where = f"{_trim(lat, 4)}, {_trim(lon, 4)}"
    amb = int(pos["ambiguity"])  # type: ignore[arg-type]
    if amb:
        nearest = {1: "0.1 minute", 2: "minute", 3: "10 minutes"}.get(amb, "degree")
        return f"{where} — to the nearest {nearest}"
    return where


def _extensions(rest: str) -> tuple[list[Field], str]:
    """The fixed-width extensions that may follow a position, and what is left over."""
    out: list[Field] = []
    m = re.match(r"^(\d{3})/(\d{3})", rest)
    if m:
        course, speed = int(m.group(1)), int(m.group(2))
        out.append(Field("Course and speed", f"{course}° at {speed} knots", m.group(0)))
        rest = rest[7:]
    m = re.match(r"^PHG(\d)(\d)(\d)(\d)", rest)
    if m:
        power, height, gain, direction = (int(x) for x in m.groups())
        aim = "omnidirectional" if direction == 0 else f"{direction * 45}°"
        out.append(
            Field(
                "Power, height, gain",
                f"{power * power} W at {10 * 2**height} ft, {gain} dB, {aim}",
                m.group(0),
            )
        )
        rest = rest[7:]
    m = re.match(r"^RNG(\d{4})", rest)
    if m:
        out.append(Field("Range", f"{int(m.group(1))} miles", m.group(0)))
        rest = rest[7:]
    # SIX digits exactly, and a leading minus is legal. A greedy `\d+` reads the real
    # frame `/A=00000070cm MMDVM…` as seventy million feet.
    m = re.search(r"/A=(-?\d{6})", rest)
    if m:
        feet = int(m.group(1))
        out.append(Field("Altitude", f"{feet:,} ft ({round(feet * 0.3048):,} m)", m.group(0)))
        rest = rest[: m.start()] + rest[m.end() :]
    return out, rest.strip()


# Weather fields, by their tag. Width matters: a value must be exactly this many
# characters AND numeric, or the scan stops — otherwise the trailing software code `tU2k`
# on a real frame here is read as a second temperature and clobbers `t078`.
_WX = (
    ("c", "Wind direction", "deg", 3),
    ("s", "Wind speed", "mph", 3),
    ("g", "Gust", "mph", 3),
    ("t", "Temperature", "f", 3),
    ("r", "Rain, last hour", "in100", 3),
    ("p", "Rain, last 24 hours", "in100", 3),
    ("P", "Rain, since midnight", "in100", 3),
    ("h", "Humidity", "pct", 2),
    ("b", "Pressure", "mb10", 5),
    ("L", "Luminosity", "wm2", 3),
    ("l", "Luminosity", "wm2", 3),
)
_WX_BY_TAG = {tag: (name, unit, width) for tag, name, unit, width in _WX}


def _weather_value(unit: str, raw: str) -> str | None:
    try:
        n = float(raw)
    except ValueError:
        return None
    if unit == "mph":
        return f"{int(n)} mph"
    if unit == "f":
        return f"{int(n)} °F ({_trim((n - 32) * 5 / 9)} °C)"
    if unit == "in100":
        return f"{n / 100:.2f} in"
    if unit == "pct":
        # `h00` means 100 %, not zero. A weather station reading 0 % humidity would be a
        # remarkable claim and it is never the one being made.
        return f"{100 if int(n) == 0 else int(n)} %"
    if unit == "mb10":
        return f"{n / 10:.1f} hPa ({_trim(n / 10 * 0.02953, 2)} inHg)"
    if unit == "deg":
        return f"from the {_compass(n)} ({int(n)}°)"
    return f"{int(n)} W/m²"


def _weather(body: str) -> tuple[list[Field], str, list[str]]:
    """Every weather field present, in the order the spec lists them rather than the
    order they arrived — a station may send them in any order, and two real stations here
    disagree (`338/000g000t078…` against `c346s000g000t078…`)."""
    fields: list[Field] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    m = re.match(r"^(\d{3})/(\d{3})", body)
    if m:
        seen["c"], seen["s"] = m.group(1), m.group(2)
        body = body[7:]
    at = 0
    while at < len(body):
        spec = _WX_BY_TAG.get(body[at])
        if spec is None:
            break
        _name, _unit, width = spec
        value = body[at + 1 : at + 1 + width]
        if not re.fullmatch(rf"-?[\d. ]{{{width}}}", value):
            break
        seen[body[at]] = value
        at += 1 + width
    trailing = body[at:].strip()
    direction, speed = seen.pop("c", None), seen.pop("s", None)
    if direction is not None or speed is not None:
        parts = []
        if direction is not None and (shown := _weather_value("deg", direction)):
            parts.append(shown)
        if speed is not None and (shown := _weather_value("mph", speed)):
            parts.append(f"at {shown}")
        fields.append(Field("Wind", " ".join(parts), f"{direction or ''}/{speed or ''}"))
    for tag, name, unit, _width in _WX:
        if tag not in seen:
            continue
        shown = _weather_value(unit, seen[tag])
        if shown is not None:
            fields.append(Field(name, shown, tag + seen[tag]))
    return fields, trailing, warnings


def _mic_e(payload: bytes, dest: str) -> tuple[list[Field], str, str, list[str]]:
    """Mic-E: `(fields, comment, symbol, warnings)`.

    The hard one, and the reason `Heard` carries `dest` and `payload` at all. Half the
    latitude, the N/S and E/W flags and the message bits live in the DESTINATION
    callsign; the longitude, speed, course and symbol are offset-28 encoded in the info
    field, two bytes of which are legitimately control characters."""
    if len(dest) < 6 or len(payload) < 9:
        return [], "", "", ["Mic-E frame too short to read"]
    digits, bits, north, offset, west = "", [], False, 0, False
    for i, char in enumerate(dest[:6]):
        if "0" <= char <= "9":
            digit, one, standard = char, 0, None
        elif "A" <= char <= "J":
            digit, one, standard = chr(ord(char) - 17), 1, False
        elif "P" <= char <= "Y":
            digit, one, standard = chr(ord(char) - 32), 1, True
        elif char == "K":
            digit, one, standard = " ", 1, False
        elif char == "L":
            digit, one, standard = " ", 0, None
        elif char == "Z":
            digit, one, standard = " ", 1, True
        else:
            return [], "", "", ["destination is not a Mic-E callsign"]
        digits += digit
        if i < 3:
            bits.append(one)
        elif i == 3:
            north = bool(standard)
        elif i == 4:
            offset = 100 if standard else 0
        else:
            west = bool(standard)

    try:
        lat = int(digits[0:2]) + float(f"{digits[2:4]}.{digits[4:6]}".replace(" ", "0")) / 60
    except ValueError:
        return [], "", "", ["Mic-E latitude is unreadable"]
    if not north:
        lat = -lat
    degrees = payload[1] - 28 + offset
    if 180 <= degrees <= 189:
        degrees -= 80
    elif 190 <= degrees <= 199:
        degrees -= 190
    minutes = payload[2] - 28
    if minutes >= 60:
        minutes -= 60
    lon = degrees + (minutes + (payload[3] - 28) / 100) / 60
    if west:
        lon = -lon
    speed = (payload[4] - 28) * 10
    tens, hundreds = divmod(payload[5] - 28, 10)
    speed += tens
    course = hundreds * 100 + (payload[6] - 28)
    if speed >= 800:
        speed -= 800
    if course >= 400:
        course -= 400

    symbol = chr(payload[8]) + chr(payload[7])
    tail = payload[9:].decode("latin-1", "replace")
    device = ""
    for suffix, name in _MICE_DEVICES.items():
        if tail.endswith(suffix):
            device, tail = name, tail[: -len(suffix)]
            break
    altitude = None
    found = re.search(r"(.)(.)(.)\}", tail)
    if found:
        a, b, c = (ord(x) - 33 for x in found.groups())
        altitude = a * 8281 + b * 91 + c - 10000
        tail = tail[: found.start()] + tail[found.end() :]
    # A leading ` or ' on the status text is the device-identification prefix, not text.
    tail = tail[1:] if tail[:1] in ("`", "'") else tail

    ambiguity = digits.count(" ")
    where = f"{_trim(lat, 4)}, {_trim(lon, 4)}"
    if ambiguity:
        where += f" — to the nearest {'minute' if ambiguity <= 2 else '10 minutes'}"
    fields = [
        Field("Position", where, dest[:6]),
        Field("Status", _MICE_STANDARD.get((bits[0] << 2) | (bits[1] << 1) | bits[2], "unknown")),
        Field("Symbol", symbols.label(symbol[0], symbol[1]), symbol),
    ]
    if speed or course:
        fields.append(
            Field(
                "Moving",
                f"{speed} knots ({round(speed * 1.151)} mph) heading {course}° "
                f"({_compass(course)})",
            )
        )
    else:
        fields.append(Field("Moving", "stationary, course not given"))
    if altitude is not None:
        fields.append(Field("Altitude", f"{altitude} m ({round(altitude * 3.281)} ft)"))
    if device:
        fields.append(Field("Radio", device))
    return fields, tail.strip(), symbol, []


def _telemetry(text: str, definitions: dict[str, list[str]]) -> tuple[list[Field], list[str]]:
    """Five analogue channels and eight bits — which mean NOTHING without the station's
    own PARM/UNIT/EQNS messages. With them, `A3 = 11` becomes "110 packets digipeated in
    the last hour"; without them the honest card shows the raw numbers and says so."""
    m = re.match(r"^T#(\w{1,5}),(.*)$", text)
    if not m:
        return [], ["not a telemetry frame"]
    values = m.group(2).split(",")
    analogue, bits = values[:5], values[5] if len(values) > 5 else ""
    names = definitions.get("PARM", [])
    units = definitions.get("UNIT", [])
    equations = definitions.get("EQNS", [])
    fields = [Field("Sequence", m.group(1))]
    for i, raw in enumerate(analogue):
        name = names[i] if i < len(names) else f"Channel {i + 1}"
        unit = units[i] if i < len(units) else ""
        if not _TELEMETRY_VALUE.match(raw.strip()):
            fields.append(Field(name, f"{raw} — unreadable", raw))
            continue
        number = float(raw)
        if len(equations) >= (i + 1) * 3:
            try:
                a, b, c = (float(x) for x in equations[i * 3 : i * 3 + 3])
            except ValueError:
                fields.append(Field(name, f"{raw}{' ' + unit if unit else ''}", raw))
                continue
            fields.append(
                Field(name, f"{_trim(a * number * number + b * number + c, 2)} {unit}".strip(), raw)
            )
        else:
            fields.append(Field(name, f"{raw}{' ' + unit if unit else ''}", raw))
    if bits:
        labels = names[5:13] if len(names) > 5 else []
        on = [labels[i] if i < len(labels) else f"B{i + 1}" for i, b in enumerate(bits) if b == "1"]
        fields.append(Field("Digital bits", ", ".join(on) + " on" if on else "all off", bits))
    warnings = [] if names else ["This station has not published what its channels measure."]
    return fields, warnings


def collect_definitions(frames: list[Heard]) -> dict[str, dict[str, list[str]]]:
    """Telemetry channel definitions, per station, from the messages that carry them.

    **Self-definitions only.** These are ordinary APRS messages: anyone with a transmitter
    can send `:K4KSC-12 :EQNS.0,1000000,0` and make a card display an invented voltage. A
    definition counts only when the sender's base callsign equals the addressee's — which
    is what real stations do, and what happens on this channel. Even then the card labels
    the names as the station's own claim."""
    out: dict[str, dict[str, list[str]]] = {}
    for heard in frames:
        if heard.kind is not MESSAGE:
            continue
        m = re.match(r"^:(.{9}):(PARM|UNIT|EQNS|BITS)\.(.*)$", heard.text)
        if m is None:
            continue
        addressee = m.group(1).strip().upper().split("-")[0]
        if addressee != (heard.origin or "").upper().split("-")[0]:
            continue
        # PARM/UNIT/BITS describe 5 analogue + 8 digital channels; EQNS carries THREE
        # coefficients for each of the 5 analogue ones. One cap for both drops the last
        # channel's equation and shows a raw count for a station that did publish one.
        cap = 15 if m.group(2) == "EQNS" else 13
        out.setdefault(heard.origin, {})[m.group(2)] = m.group(3).split(",")[:cap]
    return out


def _summarise(kind_label: str, fields: list[Field], comment: str) -> str:
    """One line, in the app's words — or nothing when the station's own text says it.

    This is the ROW, not a subtitle, so it has to stand alone. It also never repeats the
    symbol on its own: the icon already carries that, and the binding spec's rule is that
    the icon is not restated as text."""
    by_name = {f.name: f.value for f in fields}
    if kind_label == "Weather":
        parts = [by_name.get("Temperature", "").split(" (")[0], by_name.get("Wind", "")]
        rain = by_name.get("Rain, last 24 hours")
        if rain and rain != "0.00 in":
            parts.append(f"{rain} of rain in 24 hours")
        if humidity := by_name.get("Humidity"):
            parts.append(f"{humidity} humidity")
        return ", ".join(p for p in parts if p)
    if kind_label == "Telemetry":
        named = [f for f in fields if f.name not in ("Sequence", "Digital bits")]
        if not named:
            return "telemetry, with no readings"
        return ", ".join(f"{f.name} {f.value}" for f in named[:3])
    if kind_label == "Object":
        state = by_name.get("State", "")
        name = by_name.get("Object", "object")
        what = "killed" if state.startswith("killed") else by_name.get("Symbol", "object")
        return f"{name} — {what}"
    if kind_label.startswith("Position"):
        moving = by_name.get("Moving", "")
        symbol = by_name.get("Symbol", "position")
        if moving and not moving.startswith("stationary"):
            line = f"{symbol} — {moving}"
            status = by_name.get("Status")
            return f"{line}, {status}" if status and status != "unknown" else line
        return symbol
    if kind_label == "Capabilities":
        return ", ".join(
            f"{f.name} {f.value}" if f.name != "Capability" else f.value for f in fields
        )
    if kind_label == "Message":
        return (
            by_name.get("Defines")
            and f"telemetry definition for {by_name.get('To', '')}"
            or (
                f"to {by_name.get('To', '')}: {by_name.get('Text', '')}"
                if by_name.get("Text")
                else ""
            )
        )
    return ""


def explain(heard: Heard, *, definitions: dict[str, list[str]] | None = None) -> Explained:
    """What this frame says. Total: any input yields an `Explained`, never an exception."""
    try:
        return _explain(heard, definitions or {})
    except Exception as exc:  # noqa: BLE001 — one crafted frame must not end the drain
        return Explained(
            summary="",
            comment=heard.text,
            warnings=[f"This frame could not be read ({type(exc).__name__})."],
        )


def _kind_label(heard: Heard) -> str:
    if heard.dti in ("`", "'", "\x1c", "\x1d"):
        return "Position (Mic-E)"
    if heard.text.startswith("T#"):
        return "Telemetry"
    if heard.text.startswith(">"):
        return "Status"
    if heard.text.startswith("<"):
        return "Capabilities"
    if heard.kind in (WEATHER, OBJECT, MESSAGE, POSITION):
        return str(heard.kind)
    return "Other"


def _explain(heard: Heard, definitions: dict[str, list[str]]) -> Explained:
    label = _kind_label(heard)
    fields: list[Field] = []
    warnings: list[str] = []
    comment = ""
    symbol = ""
    text = heard.text

    if label == "Position (Mic-E)":
        fields, comment, symbol, warnings = _mic_e(heard.payload, heard.dest)
    elif label == "Weather":
        body = text
        if m := re.match(r"^[@/](\d{6})[zh/]", body):
            fields.append(Field("Reported at", _claimed_time(m.group(1)), m.group(0)))
            body = body[8:]
        if body[:1].isdigit() and (pos := _position(body)):
            fields.append(Field("Position", _place(pos), body[: -len(str(pos["rest"])) or None]))
            fields.append(
                Field(
                    "Symbol",
                    symbols.label(str(pos["table"]), str(pos["code"])),
                    f"{pos['table']}{pos['code']}",
                )
            )
            symbol = f"{pos['table']}{pos['code']}"
            body = str(pos["rest"])
        elif text.startswith("_"):
            if m := re.match(r"^_(\d{8})", text):
                stamp = m.group(1)
                fields.append(
                    Field(
                        "Reported at",
                        f"{stamp[0:2]}-{stamp[2:4]} {stamp[4:6]}:{stamp[6:8]} UTC, "
                        "by the station's own clock",
                        m.group(0),
                    )
                )
                body = text[9:]
            warnings.append(
                "No position in this frame — this station's location comes from a separate beacon."
            )
        weather, comment, more = _weather(body)
        fields += weather
        warnings += more
    elif label == "Telemetry":
        fields, warnings = _telemetry(text, definitions)
    elif label == "Object":
        if m := re.match(r"^;(.{9})([*_])(\d{6})[zh/]", text):
            fields.append(Field("Object", m.group(1).rstrip(), m.group(1)))
            fields.append(
                Field(
                    "State",
                    "live" if m.group(2) == "*" else "killed — remove it from the map",
                    m.group(2),
                )
            )
            fields.append(Field("Reported at", _claimed_time(m.group(3)), m.group(3)))
            if pos := _position(text[m.end() :]):
                fields.append(Field("Position", _place(pos)))
                symbol = f"{pos['table']}{pos['code']}"
                fields.append(Field("Symbol", symbols.label(symbol[0], symbol[1]), symbol))
                extra, comment = _extensions(str(pos["rest"]))
                fields += extra
            warnings.append("Object names are not owned — any station may transmit the same name.")
    elif label == "Message":
        if m := re.match(r"^:(.{9}):(.*)$", text):
            fields.append(Field("To", m.group(1).strip(), m.group(1)))
            if definition := re.match(r"^(PARM|UNIT|EQNS|BITS)\.(.*)$", m.group(2)):
                fields.append(Field("Kind", f"{definition.group(1)} — telemetry definition"))
                fields.append(Field("Defines", definition.group(2)))
            else:
                fields.append(Field("Text", m.group(2)))
    elif label == "Position":
        body = text
        if m := re.match(r"^[@/](\d{6})[zh/]", body):
            fields.append(Field("Reported at", _claimed_time(m.group(1)), m.group(0)))
            body = body[8:]
        else:
            body = body[1:]
        if pos := _position(body):
            fields.append(Field("Position", _place(pos)))
            symbol = f"{pos['table']}{pos['code']}"
            fields.append(Field("Symbol", symbols.label(symbol[0], symbol[1]), symbol))
            extra, comment = _extensions(str(pos["rest"]))
            fields += extra
        else:
            comment = body
            warnings.append("The position in this frame could not be read.")
    elif label == "Status":
        comment = text[1:]
    elif label == "Capabilities":
        for part in text[1:].split(","):
            name, sep, value = part.partition("=")
            fields.append(Field(name, value) if sep else Field("Capability", name))
    else:
        comment = text
        if heard.dest.upper() == "BEACON":
            label = "AX.25 beacon"
            warnings.append("Not an APRS frame — a plain AX.25 beacon addressed to BEACON.")

    return Explained(
        summary=_summarise(label, fields, comment),
        fields=fields,
        comment=comment,
        symbol=symbol,
        warnings=warnings,
    )


def _claimed_time(stamp: str) -> str:
    """A timestamp a station sent, said as a claim.

    Not pedantry: `N1KSC-1` transmits `@290303z` on 3 September, so its day counter is
    simply wrong. Presenting that as the time would contradict the heard time sitting
    beside it on the same row."""
    return f"day {stamp[0:2]}, {stamp[2:4]}:{stamp[4:6]} UTC, by the station's own clock"


def kind_label(heard: Heard) -> str:
    """What the row's title calls this frame."""
    return _kind_label(heard)
