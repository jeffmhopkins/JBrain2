"""The box's half of the panel notice path, with no I/O (JPANEL_C6_PLAN.md §2, wave C3b).

Jerv, Ops or a workflow puts a notice on the owner's desk panel: a title and a few lines,
optionally a picture the sender drew, optionally a short sound, optionally a question with up
to four answers that go back to the sender. This module is what can be decided without a
database or a network: the limits, the refusal rule, the picture fitted to the glass and
encoded the way the panel draws it, the sound as the panel plays it, and the `JN1` stream that
carries all of it. The I/O is `notice_service.py`; the routes `api/panel_notice.py`.

THE REFUSAL IS AT THE DOOR. The owner: *"It would reject it and return the API response that
the device is not online or not accepting push."* The panel reports its own switches and night
window on the debug pickup it makes every 3 s (`record_prefs`), so the box can say no at send
time from what the panel said a few seconds ago — never from a guess, and never by holding a
notice to pop up later.

THE PICTURE IS ENCODED ONCE, ON THE BOX. The C6 has no PSRAM and no room for a 460 KB frame,
so the panel never decodes a PNG: it takes rgb565 run-length stripes — the runs of the `JPS1`
screenshot (`debug_relay.decode_jps1`), the same `(len-1, rgb565)` pairs, run length ≤ 128 —
writes them to its `storage` partition as they arrive and draws a stripe at a time.
"""

from __future__ import annotations

import array
import io
import re
import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

# ---- limits ------------------------------------------------------------------------------

#: What the panel's notice card holds. The title is two lines at 30 px, the body the rest of
#: the card; both are cut nowhere on the panel, so they are refused here instead.
TITLE_BYTES = 60
BODY_BYTES = 300
#: The panel draws at most four answer buttons in one row of 416 px.
CHOICES_MAX = 4
CHOICE_BYTES = 16
#: A notice that hides itself does so after this long once shown. Under three seconds it
#: cannot be read; past ten minutes it is "until tapped" with extra steps.
HIDE_MIN_S = 3
HIDE_MAX_S = 600
#: A plain notice the sender gave no time hides after this long; a question waits for its answer
#: (owner, 2026-10-13). A sender asks for "until tapped" with `hide_after_s=0`.
HIDE_DEFAULT_PLAIN_S = 20
HIDE_UNTIL_TAPPED = 0
#: How long an answer is accepted once the panel has the notice. Default: fifteen minutes,
#: long enough to walk back to the desk, short enough that a stale "run the cleanup now?" is
#: not answered tomorrow.
TTL_MIN_S = 30
TTL_MAX_S = 24 * 3600
TTL_DEFAULT_S = 15 * 60
#: A notice not collected by then is expired: the panel was online at send time (it had
#: picked up within OFFLINE_AFTER_S), so a minute is twenty pickups.
PICKUP_WINDOW_S = 60
#: How many notices may be open on one panel at once (queued, delivered or shown). The panel
#: shows "1 of N"; more than four waiting is a sender looping, not news.
MAX_OPEN = 4
#: Finished rows (and their picture and sound) are deleted this long after they finish.
RESULT_TTL_S = 24 * 3600
#: The long-poll ceiling, under the tunnel's ~100 s edge limit with a wide margin.
MAX_WAIT_S = 25

#: The glass.
SCREEN = 480
#: A banner keeps at least half the card for the title, body and answers.
BANNER_MAX_H = 240
#: What a sender may upload. Generous for a drawn banner or a photo; the encoded size is
#: what the panel pays for, and that has its own cap below.
IMAGE_UPLOAD_MAX = 8 * 1024 * 1024
IMAGE_PIXELS_MAX = 40_000_000
IMAGE_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
#: The encoded picture, as it lands in the panel's `storage` partition (3.75 MB, shared
#: with everything else the panel caches). Four open notices, each at both caps, is under
#: 2.5 MB. A drawn banner is a few KB; a photograph is the case this bounds.
IMAGE_STREAM_MAX = 320 * 1024
#: Stripe rows match the panel's 480x40 draw buffer, so one stripe decodes into one buffer.
STRIPE_ROWS_MAX = 40
#: The JPS1 run limit (`dbg_shot.c` RUN_MAX).
RUN_MAX = 128

#: The sound. The owner: *"a short 8 second or less audio clip"*.
AUDIO_MAX_MS = 8000
AUDIO_UPLOAD_MAX = 4 * 1024 * 1024
PANEL_RATE = 16000
#: Text spoken in the PWA's voice; anything this long already overruns eight seconds.
SAY_CHARS = 200

#: §2's rule, shared with the debug relay: not heard from in 30 s is offline.
OFFLINE_AFTER_S = 30

Sender = str
_SENDER = re.compile(r"^(jerv|ops|owner|debug|workflow:[a-z0-9][a-z0-9_.-]{0,47})$")
#: What the panel shows as the sender, at most this many bytes.
SENDER_LABEL_BYTES = 32

States = Literal["queued", "delivered", "shown", "answered", "dismissed", "expired"]
OPEN_STATES = ("queued", "delivered", "shown")
FINAL_STATES = ("answered", "dismissed", "expired")


class NoticeRefused(ValueError):
    """A notice the box will not take. `reason` is the wire value; `status` the HTTP one."""

    def __init__(self, reason: str, status: int, **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"reason": self.reason, **self.extra}


def check_sender(sender: str) -> str:
    if not _SENDER.match(sender):
        raise NoticeRefused("bad_sender", 422, want="jerv | ops | owner | debug | workflow:<name>")
    return sender


def sender_label(sender: str) -> str:
    """How the panel names the sender: "Jerv", "Ops", or the workflow's own name."""
    if sender.startswith("workflow:"):
        name = sender.split(":", 1)[1].replace("_", " ").replace("-", " ").strip()
        return _cut(name[:1].upper() + name[1:], SENDER_LABEL_BYTES)
    return {"jerv": "Jerv", "ops": "Ops", "owner": "You", "debug": "Debug"}[sender]


def _cut(text: str, limit: int) -> str:
    raw = text.encode()
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", "ignore")


def _plain(field: str, value: str, limit: int, *, required: bool) -> str:
    value = value.strip()
    if required and not value:
        raise NoticeRefused("bad_field", 422, field=field, want="non-empty")
    if len(value.encode()) > limit:
        raise NoticeRefused("too_long", 422, field=field, max_bytes=limit)
    # The panel's fonts have no glyphs for control characters; a stray one is a box drawn.
    if any(ord(c) < 0x20 and c != "\n" for c in value):
        raise NoticeRefused("bad_field", 422, field=field, want="printable text")
    return value


def check_text(title: str, body: str, choices: list[str]) -> tuple[str, str, list[str]]:
    title = _plain("title", title, TITLE_BYTES, required=True)
    if "\n" in title:
        raise NoticeRefused("bad_field", 422, field="title", want="one line")
    body = _plain("body", body, BODY_BYTES, required=False)
    if len(choices) > CHOICES_MAX:
        raise NoticeRefused("too_many_choices", 422, max=CHOICES_MAX)
    out = [_plain("choices", c, CHOICE_BYTES, required=True) for c in choices]
    if any("\n" in c for c in out):
        raise NoticeRefused("bad_field", 422, field="choices", want="one line")
    if len(set(out)) != len(out):
        # The answer is stored as the label, so two buttons that read the same are one answer.
        raise NoticeRefused("bad_field", 422, field="choices", want="distinct labels")
    return title, body, out


def check_hide(hide_after_s: int | None, *, question: bool) -> int | None:
    """The hide time the notice is stored with, None for until tapped. Omitted (None) is the
    default — HIDE_DEFAULT_PLAIN_S for a plain notice, none for a question; 0 asks for until
    tapped whatever the kind. Callers that predate the default and send None for a plain notice
    now get it, which is the owner's call, not a break: nothing else about the notice changes."""
    if hide_after_s is None:
        return None if question else HIDE_DEFAULT_PLAIN_S
    if hide_after_s == HIDE_UNTIL_TAPPED:
        return None
    if not HIDE_MIN_S <= hide_after_s <= HIDE_MAX_S:
        raise NoticeRefused(
            "bad_field", 422, field="hide_after_s", min=HIDE_MIN_S, max=HIDE_MAX_S, until_tapped=0
        )
    return hide_after_s


def check_ttl(ttl_s: int | None) -> int:
    ttl = TTL_DEFAULT_S if ttl_s is None else ttl_s
    if not TTL_MIN_S <= ttl <= TTL_MAX_S:
        raise NoticeRefused("bad_field", 422, field="ttl_s", min=TTL_MIN_S, max=TTL_MAX_S)
    return ttl


# ---- what the panel reported: its switches, its night window, and when ---------------------

#: Mirror of the zone table in `firmware-c6/components/svc_rtc/time_logic.c`, in its order —
#: the panel reports an index (`tz`), and the night window is in the panel's own time.
#: `test_panel_notice.py` parses the C table and fails when the two disagree.
PANEL_ZONES: tuple[tuple[str, str], ...] = (
    ("Honolulu", "Pacific/Honolulu"),
    ("Anchorage", "America/Anchorage"),
    ("Los Angeles", "America/Los_Angeles"),
    ("Denver", "America/Denver"),
    ("Chicago", "America/Chicago"),
    ("New York", "America/New_York"),
    ("Phoenix", "America/Phoenix"),
    ("UTC", "UTC"),
    ("London", "Europe/London"),
    ("Paris", "Europe/Paris"),
    ("Athens", "Europe/Athens"),
    ("Sydney", "Australia/Sydney"),
)


@dataclass(frozen=True)
class NoticePrefs:
    """`notif_accept`, `notif_quiet`, `notif_chime`, `night_from`, `night_to`, `tz` as the
    panel last reported them (firmware-c6 `svc_settings` names)."""

    accept: bool
    quiet: bool
    chime: bool
    night_from: int  # minutes after local midnight
    night_to: int
    tz: int  # index into PANEL_ZONES

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(PANEL_ZONES[self.tz][1])

    def as_dict(self) -> dict[str, Any]:
        return {
            "accept": self.accept,
            "quiet": self.quiet,
            "chime": self.chime,
            "night_from": hhmm(self.night_from),
            "night_to": hhmm(self.night_to),
            "tz": PANEL_ZONES[self.tz][1],
        }


#: The query parameters the panel's pickup carries them in (wire names are short because the
#: firmware builds the URL in a fixed buffer).
PREF_PARAMS = ("na", "nq", "nc", "nf", "nt", "tz")


def parse_prefs(params: dict[str, str]) -> NoticePrefs | None:
    """The panel's report, or None when it sent none (firmware before C3b) or any field is
    malformed — a half-understood report would refuse or admit on a guess."""
    if not all(k in params for k in PREF_PARAMS):
        return None
    try:
        na, nq, nc, nf, nt, tz = (int(params[k]) for k in PREF_PARAMS)
    except ValueError:
        return None
    if {na, nq, nc} - {0, 1} or not (0 <= nf < 1440 and 0 <= nt < 1440):
        return None
    if not 0 <= tz < len(PANEL_ZONES):
        return None
    return NoticePrefs(bool(na), bool(nq), bool(nc), nf, nt, tz)


# device_id -> what its last pickup reported. In memory for the reason `debug_relay._seen` is:
# the panel says it again every 3 s, so a box restart costs one pickup of "no notice surface".
_prefs: dict[str, NoticePrefs] = {}


def record_prefs(device_id: str, prefs: NoticePrefs | None) -> None:
    """Keep a pickup's report. A pickup with none (an older image after a rollback) clears it,
    so the box never decides from switches a panel no longer has."""
    if prefs is None:
        _prefs.pop(device_id, None)
    else:
        _prefs[device_id] = prefs


def prefs_for(device_id: str) -> NoticePrefs | None:
    return _prefs.get(device_id)


def forget_prefs(device_id: str | None = None) -> None:
    if device_id is None:
        _prefs.clear()
    else:
        _prefs.pop(device_id, None)


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_window(minute: int, start: int, end: int) -> bool:
    """Whether a local minute-of-day falls in [start, end), wrapping midnight. An empty
    window (start == end) is never — the panel's own `inNight` reads it the same way."""
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def quiet_until(prefs: NoticePrefs, now: datetime) -> datetime | None:
    """When the panel's night window ends, if `now` is inside it and it refuses during it."""
    if not prefs.quiet:
        return None
    local = now.astimezone(prefs.zone)
    minute = local.hour * 60 + local.minute
    if not in_window(minute, prefs.night_from, prefs.night_to):
        return None
    end = local.replace(
        hour=prefs.night_to // 60, minute=prefs.night_to % 60, second=0, microsecond=0
    )
    if end <= local:
        end += timedelta(days=1)
    # Re-resolve on the wall clock so a window that crosses a DST change ends at 06:30 local,
    # not 06:30 plus or minus the hour the night gained or lost.
    end = end.replace(tzinfo=None).replace(tzinfo=prefs.zone)
    return end.astimezone(UTC)


def refusal(
    seen: tuple[float, str] | None, prefs: NoticePrefs | None, now: datetime
) -> NoticeRefused | None:
    """§2's refusal table, in the order a sender can act on it.

    `seen` is `debug_relay.last_seen` (seconds since the panel last picked up, its board);
    `prefs` what it reported on that pickup. Offline first, because a panel that is not
    there has no switches worth reading."""
    if seen is None or seen[0] > OFFLINE_AFTER_S:
        return NoticeRefused("offline", 503, last_seen_s=int(seen[0]) if seen else None)
    if prefs is None:
        # Online, but its firmware reports no notice settings, so it has nothing to show one
        # with (every image before C3b's panel half). Not `not_accepting`: the owner did not
        # switch anything off, and the remedy is an update, not a setting.
        return NoticeRefused("no_notice_surface", 409, board=seen[1])
    if not prefs.accept:
        return NoticeRefused("not_accepting", 409)
    until = quiet_until(prefs, now)
    if until is not None:
        return NoticeRefused(
            "quiet_hours",
            409,
            until=hhmm(prefs.night_to),
            until_at=until.isoformat(),
            tz=PANEL_ZONES[prefs.tz][1],
        )
    return None


# ---- the picture -------------------------------------------------------------------------

ImageMode = Literal["full", "banner"]


@dataclass(frozen=True)
class FittedImage:
    mode: ImageMode
    width: int
    height: int
    #: The picture as STRIPE records' payloads (`u16 y0, u16 rows, runs`), top to bottom.
    stripes: tuple[bytes, ...]
    #: How far the colours were reduced to fit IMAGE_STREAM_MAX: 0 exact rgb565, then 4 and
    #: 3 bits a channel.
    posterize: int

    @property
    def stream_bytes(self) -> int:
        return sum(3 + len(s) for s in self.stripes)

    def blob(self) -> bytes:
        """The stored form: the STRIPE records exactly as they go on the wire."""
        return b"".join(record(STRIPE, s) for s in self.stripes)


def default_mode(body: str, choices: list[str]) -> ImageMode:
    """A picture with nothing to say beside it fills the glass; one with words or answers is
    a banner over them. Documented in the plan §2 as the default."""
    return "full" if not body and not choices else "banner"


def _open(data: bytes) -> Any:
    from PIL import Image, ImageOps  # noqa: PLC0415 - only where a picture is sent

    if len(data) > IMAGE_UPLOAD_MAX:
        raise NoticeRefused("image_too_big", 413, max_bytes=IMAGE_UPLOAD_MAX)
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt not in IMAGE_FORMATS:
            raise NoticeRefused("image_format", 415, want=sorted(IMAGE_FORMATS), got=fmt)
        if img.width * img.height > IMAGE_PIXELS_MAX:
            raise NoticeRefused("image_too_big", 413, max_pixels=IMAGE_PIXELS_MAX)
        img.load()
    except NoticeRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — Pillow raises a dozen types for a bad file
        raise NoticeRefused("image_unreadable", 422, why=type(exc).__name__) from None
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P", "PA"):
        # Transparent means unlit: the ground of every notice is AMOLED black.
        img = img.convert("RGBA")
        ground = Image.new("RGBA", img.size, (0, 0, 0, 255))
        img = Image.alpha_composite(ground, img)
    return img.convert("RGB")


def _contain(img: Any, box_w: int, box_h: int, out_w: int, out_h: int) -> Any:
    """Scale `img` to fit inside box_w x box_h keeping its shape, centred on black in an
    out_w x out_h canvas. A picture already that size passes through untouched, so a sender
    that draws its own 480 px banner gets exactly its pixels."""
    from PIL import Image  # noqa: PLC0415

    scale = min(box_w / img.width, box_h / img.height)
    w = max(1, min(box_w, round(img.width * scale)))
    h = max(1, min(box_h, round(img.height * scale)))
    if (w, h) != img.size:
        img = img.resize((w, h), Image.Resampling.LANCZOS)
    if (w, h) == (out_w, out_h):
        return img
    canvas = Image.new("RGB", (out_w, out_h), (0, 0, 0))
    canvas.paste(img, ((out_w - w) // 2, (out_h - h) // 2))
    return canvas


def to_rgb565(img: Any) -> list[int]:
    raw = img.tobytes()
    return [
        ((raw[i] >> 3) << 11) | ((raw[i + 1] >> 2) << 5) | (raw[i + 2] >> 3)
        for i in range(0, len(raw), 3)
    ]


_POSTER_MASKS = {0: 0xFFFF, 4: 0xF79E, 3: 0xE71C}


def runs(px: list[int]) -> bytes:
    """`(len-1, rgb565 LE)` runs of at most RUN_MAX — byte for byte what `dbg_shot.c` emits
    for the same pixels."""
    out = bytearray()
    i, n = 0, len(px)
    while i < n:
        k = 1
        while i + k < n and k < RUN_MAX and px[i + k] == px[i]:
            k += 1
        out += struct.pack("<BH", k - 1, px[i])
        i += k
    return bytes(out)


def stripes(px: list[int], width: int, height: int) -> tuple[bytes, ...]:
    """Cut the frame into STRIPE payloads: as many whole rows as fit one record, never more
    than STRIPE_ROWS_MAX. Runs are greedy over the stripe's pixels in order, so one may cross
    a row but never a stripe — exactly `runs()` of the stripe, built a row at a time."""
    room = MAX_RECORD - 4
    out: list[bytes] = []
    y0 = 0
    while y0 < height:
        body = bytearray()
        color, count, rows = -1, 0, 0
        while rows < STRIPE_ROWS_MAX and y0 + rows < height:
            mark = (len(body), color, count)
            start = (y0 + rows) * width
            for p in px[start : start + width]:
                if p == color and count < RUN_MAX:
                    count += 1
                else:
                    if count:
                        body += struct.pack("<BH", count - 1, color)
                    color, count = p, 1
            if len(body) + 3 > room:
                if rows == 0:
                    raise NoticeRefused("image_too_complex", 413, why="one row overflows a record")
                del body[mark[0] :]
                color, count = mark[1], mark[2]
                break
            rows += 1
        body += struct.pack("<BH", count - 1, color)
        out.append(struct.pack("<HH", y0, rows) + bytes(body))
        y0 += rows
    return tuple(out)


def fit_image(data: bytes, mode: ImageMode) -> FittedImage:
    """The sender's PNG/JPEG/WebP as the panel draws it.

    `full`: contained in 480x480, letterboxed on black, always 480x480. `banner`: 480 wide,
    as tall as the picture's shape makes it up to BANNER_MAX_H (a taller one is contained in
    480 x BANNER_MAX_H). Over IMAGE_STREAM_MAX the colours are posterized (4, then 3 bits a
    channel) — a photograph's dither is what costs runs — and past that it is refused."""
    img = _open(data)
    if mode == "full":
        img = _contain(img, SCREEN, SCREEN, SCREEN, SCREEN)
    else:
        h = max(1, min(BANNER_MAX_H, round(img.height * SCREEN / img.width)))
        img = _contain(img, SCREEN, h, SCREEN, h)
    px = to_rgb565(img)
    best = 0
    for bits, mask in _POSTER_MASKS.items():
        cut = px if bits == 0 else [p & mask for p in px]
        try:
            st = stripes(cut, img.width, img.height)
        except NoticeRefused:
            continue
        fitted = FittedImage(mode, img.width, img.height, st, bits)
        if fitted.stream_bytes <= IMAGE_STREAM_MAX:
            return fitted
        best = fitted.stream_bytes
    raise NoticeRefused(
        "image_too_complex",
        413,
        max_bytes=IMAGE_STREAM_MAX,
        bytes=best,
        why="too detailed for the panel even with fewer colours; send a simpler or smaller one",
    )


# ---- the sound ---------------------------------------------------------------------------


def pcm_ms(pcm: bytes) -> int:
    return len(pcm) * 1000 // (PANEL_RATE * 2)


def check_pcm(pcm: bytes) -> bytes:
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    if not pcm:
        raise NoticeRefused("audio_empty", 422)
    ms = pcm_ms(pcm)
    if ms > AUDIO_MAX_MS:
        raise NoticeRefused("audio_too_long", 422, ms=ms, max_ms=AUDIO_MAX_MS)
    return pcm


def wav_pcm(data: bytes) -> bytes | None:
    """A PCM WAV as 16 kHz mono s16, or None when it is not one this reads (the caller hands
    that to ffmpeg). Walks the chunks rather than assuming a 44-byte header."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt: tuple[int, int, int, int] | None = None
    pcm = b""
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        chunk = data[pos + 8 : pos + 8 + size]
        if cid == b"fmt " and len(chunk) >= 16:
            tag, channels, rate = struct.unpack_from("<HHI", chunk, 0)
            (bits,) = struct.unpack_from("<H", chunk, 14)
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            pcm = chunk
        pos += 8 + size + (size & 1)
    if fmt is None or not pcm:
        return None
    tag, channels, rate, bits = fmt
    if tag != 1 or bits != 16 or channels not in (1, 2) or not 4000 <= rate <= 192000:
        return None
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % (2 * channels)])
    if channels == 2:
        samples = array.array(
            "h", ((a + b) // 2 for a, b in zip(samples[::2], samples[1::2], strict=True))
        )
    return resample(samples.tobytes(), rate)


def resample(pcm: bytes, rate: int) -> bytes:
    """Linear resample to 16 kHz — through a 29 mm speaker nothing cleverer is audible."""
    if rate == PANEL_RATE or not pcm:
        return pcm
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    out = array.array("h")
    step = rate / PANEL_RATE
    pos = 0.0
    while pos < len(src) - 1:
        i = int(pos)
        frac = pos - i
        out.append(int(src[i] + (src[i + 1] - src[i]) * frac))
        pos += step
    return out.tobytes()


# ---- JN1: the notice on the wire, box -> panel ---------------------------------------------
#
# `JN1`, then records until END. Every record is JT2's framing (`api/panel_stream.py`):
#
#     type: u8 | length: u16 little-endian | payload: `length` bytes (≤ MAX_RECORD)
#
#     0x01 META    fixed 30 bytes, little-endian, then the sender's label (UTF-8, ≤ 32):
#                    id        16  the notice's uuid, raw bytes (echoed in the state POST)
#                    flags      u8 bit 0 chime, bit 1 question (≥ 1 CHOICE follows),
#                                  bit 2 picture (STRIPEs follow), bit 3 sound (PCMs follow)
#                    hide_s    u16 hide this long after it is shown; 0 = until tapped
#                    ttl_s     u32 the answer is accepted for this long from now
#                    img_mode   u8 0 none, 1 full (480x480), 2 banner (480 x img_h)
#                    img_w     u16 0 when there is no picture
#                    img_h     u16
#                    audio_ms  u16 0 when there is no sound
#     0x02 TITLE   UTF-8, ≤ 60 bytes, one line
#     0x03 BODY    UTF-8, ≤ 300 bytes, may hold '\n'; absent when empty
#     0x04 CHOICE  UTF-8, ≤ 16 bytes; one record per answer button, in order (index 0..3)
#     0x05 STRIPE  u16 y0 | u16 rows | runs (u8 len-1, u16 rgb565 LE) covering rows x img_w
#                  pixels exactly; top to bottom, contiguous, rows ≤ 40, runs ≤ 128
#     0x06 PCM     16 kHz mono s16le, ≤ 4096 bytes, an even count
#     0x07 END     u8 0, then u32 the byte count of everything before this record
#
# Order is fixed: META, TITLE, BODY?, CHOICE*, STRIPE*, PCM*, END. A reader skips a type it
# does not know (its length says how far); a stream without END, or whose END count
# disagrees, was torn and is discarded whole — a notice is shown complete or not at all.

MAGIC = b"JN1"
META, TITLE, BODY, CHOICE, STRIPE, PCM, END = 1, 2, 3, 4, 5, 6, 7
FLAG_CHIME, FLAG_QUESTION, FLAG_PICTURE, FLAG_SOUND = 1, 2, 4, 8
IMG_NONE, IMG_FULL, IMG_BANNER = 0, 1, 2
MAX_RECORD = 4096
PCM_RECORD = 4096
_META = struct.Struct("<16sBHIBHHH")
assert _META.size == 30


def record(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_RECORD:
        raise ValueError(f"record of {len(payload)} bytes")
    return struct.pack("<BH", kind, len(payload)) + payload


@dataclass(frozen=True)
class WireNotice:
    """What the stream carries — built from a row (`notice_service`) or parsed back (tests,
    fixtures)."""

    id: str
    sender_label: str
    title: str
    body: str
    choices: tuple[str, ...]
    chime: bool
    hide_after_s: int | None
    ttl_s: int
    image_mode: ImageMode | None = None
    image_w: int = 0
    image_h: int = 0
    audio_ms: int = 0


def encode(n: WireNotice, image_blob: bytes = b"", pcm: bytes = b"") -> bytes:
    """The whole JN1 stream. `image_blob` is `FittedImage.blob()` as stored; `pcm` the sound."""
    flags = (
        (FLAG_CHIME if n.chime else 0)
        | (FLAG_QUESTION if n.choices else 0)
        | (FLAG_PICTURE if image_blob else 0)
        | (FLAG_SOUND if pcm else 0)
    )
    mode = {None: IMG_NONE, "full": IMG_FULL, "banner": IMG_BANNER}[n.image_mode]
    meta = _META.pack(
        uuid.UUID(n.id).bytes,
        flags,
        n.hide_after_s or 0,
        n.ttl_s,
        mode if image_blob else IMG_NONE,
        n.image_w if image_blob else 0,
        n.image_h if image_blob else 0,
        n.audio_ms if pcm else 0,
    )
    parts = [MAGIC, record(META, meta + _cut(n.sender_label, SENDER_LABEL_BYTES).encode())]
    parts.append(record(TITLE, n.title.encode()))
    if n.body:
        parts.append(record(BODY, n.body.encode()))
    parts.extend(record(CHOICE, c.encode()) for c in n.choices)
    parts.append(image_blob)
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    parts.extend(record(PCM, pcm[i : i + PCM_RECORD]) for i in range(0, len(pcm), PCM_RECORD))
    head = b"".join(parts)
    return head + record(END, struct.pack("<BI", 0, len(head)))


class WireError(ValueError):
    pass


@dataclass(frozen=True)
class Decoded:
    notice: WireNotice
    #: The picture as rgb565 pixels, row-major, img_w x img_h (empty without one).
    pixels: tuple[int, ...]
    pcm: bytes
    stripes: int


def decode(buf: bytes) -> Decoded:
    """Parse a JN1 stream the way the panel must, refusing what the panel must refuse. The
    reference reader for the firmware wave and the fixtures' round trip."""
    if buf[:3] != MAGIC:
        raise WireError("not a JN1 stream")
    at = 3
    meta: tuple[Any, ...] | None = None
    sender = title = body = ""
    choices: list[str] = []
    pixels: list[int] = []
    pcm = bytearray()
    want_row, n_stripes = 0, 0
    order = -1
    while True:
        if at + 3 > len(buf):
            raise WireError("stream ends without END")
        kind, size = struct.unpack_from("<BH", buf, at)
        payload = buf[at + 3 : at + 3 + size]
        if len(payload) != size or size > MAX_RECORD:
            raise WireError(f"record at {at} cut short or too long")
        rank = {META: 0, TITLE: 1, BODY: 2, CHOICE: 3, STRIPE: 4, PCM: 5, END: 6}.get(kind)
        if rank is not None:
            if rank < order or (rank == order and kind in (META, TITLE, BODY)):
                raise WireError(f"record type {kind} out of order")
            order = rank
        if kind == META:
            if size < _META.size:
                raise WireError("META too short")
            meta = _META.unpack_from(payload)
            sender = payload[_META.size :].decode()
        elif meta is None:
            raise WireError("first record is not META")
        elif kind == TITLE:
            title = payload.decode()
        elif kind == BODY:
            body = payload.decode()
        elif kind == CHOICE:
            choices.append(payload.decode())
        elif kind == STRIPE:
            w, h = meta[5], meta[6]
            y0, rows = struct.unpack_from("<HH", payload)
            if y0 != want_row or not rows or y0 + rows > h or rows > STRIPE_ROWS_MAX:
                raise WireError(f"stripe at row {y0}, expected {want_row}")
            left = rows * w
            for i in range(4, size, 3):
                k = payload[i] + 1
                if i + 3 > size or k > left:
                    raise WireError(f"run overflows stripe at row {y0}")
                (c,) = struct.unpack_from("<H", payload, i + 1)
                pixels.extend([c] * k)
                left -= k
            if left:
                raise WireError(f"stripe at row {y0} short by {left} pixels")
            want_row = y0 + rows
            n_stripes += 1
        elif kind == PCM:
            if size % 2:
                raise WireError("odd PCM record")
            pcm += payload
        elif kind == END:
            code, count = struct.unpack_from("<BI", payload)
            if code != 0 or count != at:
                raise WireError("END does not match the stream")
            if at + 3 + size != len(buf):
                raise WireError("bytes after END")
            break
        at += 3 + size
    if meta is None:
        raise WireError("no META")
    nid, flags, hide_s, ttl_s, mode, w, h, audio_ms = meta
    if bool(flags & FLAG_PICTURE) != bool(n_stripes) or (n_stripes and want_row != h):
        raise WireError("picture flag and stripes disagree")
    if bool(flags & FLAG_QUESTION) != bool(choices) or len(choices) > CHOICES_MAX:
        raise WireError("question flag and choices disagree")
    if bool(flags & FLAG_SOUND) != bool(pcm) or (pcm and abs(pcm_ms(bytes(pcm)) - audio_ms) > 1):
        raise WireError("sound flag and PCM disagree")
    image_mode: ImageMode | None = {IMG_NONE: None, IMG_FULL: "full", IMG_BANNER: "banner"}[mode]
    return Decoded(
        WireNotice(
            id=str(uuid.UUID(bytes=nid)),
            sender_label=sender,
            title=title,
            body=body,
            choices=tuple(choices),
            chime=bool(flags & FLAG_CHIME),
            hide_after_s=hide_s or None,
            ttl_s=ttl_s,
            image_mode=image_mode,
            image_w=w,
            image_h=h,
            audio_ms=audio_ms,
        ),
        tuple(pixels),
        bytes(pcm),
        n_stripes,
    )
