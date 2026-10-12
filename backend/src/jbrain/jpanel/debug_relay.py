"""The box's half of the panel debug relay (docs/plans/JPANEL_C6_PLAN.md §4, wave C2d).

A debug token asks; the panel answers on its next pickup. This module is the part with no
I/O: the command whitelist the box checks before queueing anything, the flat request the
panel's `dbg_req_parse` accepts, the in-memory record of which panels are polling, and the
`JPS1` screenshot decoder. The routes are `api/debug_panel.py` (owner side) and
`api/panel_debug.py` (panel side).

THE WHITELIST IS WRITTEN TWICE ON PURPOSE. The panel refuses anything outside
`firmware-c6/components/svc_debug/dbg_cmd.c`'s table whatever the box sends, so the copy
here is not the security boundary — it is what lets the box answer `needs_confirm` or
`unknown_command` at the door instead of after a three-second round trip, and refuse a
`reboot` without `confirm` even if a firmware bug forgot to. `test_panel_debug_relay.py`
parses the C table and fails when the two disagree.
"""

from __future__ import annotations

import io
import struct
import time
from dataclasses import dataclass
from typing import Any

#: The only board with a debug surface. The S3 panels have telemetry and nothing that
#: answers a command, so a device that last polled as anything else is refused.
DEBUG_BOARD = "c6-amoled-216"


@dataclass(frozen=True)
class CommandInfo:
    name: str
    #: Refused without `"confirm": true`, at the box and again on the panel.
    destructive: bool
    #: Safe to retry after a lost result. Informational: the box never retries by itself.
    idempotent: bool
    available: bool = True


#: Mirror of `CMDS[]` in `firmware-c6/components/svc_debug/dbg_cmd.c`, in its order.
COMMANDS: dict[str, CommandInfo] = {
    c.name: c
    for c in (
        CommandInfo("help", False, True),
        CommandInfo("snapshot", False, True),
        CommandInfo("logs", False, True),
        CommandInfo("log_level", False, True),
        CommandInfo("open", False, True),
        CommandInfo("home", False, True),
        CommandInfo("set", False, True),
        CommandInfo("inject", False, False),
        CommandInfo("ota_check", False, True),
        CommandInfo("report_now", False, True),
        CommandInfo("screenshot", False, True),
        CommandInfo("reboot", True, False),
    )
}

#: `dbg_req_t`'s string fields and their buffer sizes less the NUL. A longer value is
#: `too_long` on the panel; refusing it here says so before it costs a round trip.
STR_ARGS: dict[str, int] = {"app": 15, "tag": 15, "event": 11, "name": 19}
#: `dbg_req_t`'s int32 fields.
INT_ARGS: frozenset[str] = frozenset({"level", "x", "y", "value"})
_INT32 = (-(2**31), 2**31 - 1)

#: A queued request the panel has not picked up by then is expired, never delivered late:
#: a `reboot` arriving five minutes after it was asked for is a different command.
PICKUP_WINDOW_S = 60
#: Once picked up, how long the panel has to answer. Set by the table's trigger, not here;
#: this copy is what the docs and tests quote. A screenshot over a slow link is the long pole.
ANSWER_WINDOW_S = 120
#: Finished rows (and their screenshots) are deleted after this.
RESULT_TTL_S = 15 * 60
#: §2's rule: a panel not heard from in this long is offline, and a command is refused at the
#: door rather than queued for a panel that may never come back.
OFFLINE_AFTER_S = 30
#: How many requests may wait for one panel. One is the normal case; more is a client
#: looping, and the queue is a debug aid, not a buffer.
MAX_QUEUED = 4
#: The long-poll ceiling: under the tunnel's ~100 s edge limit with a wide margin.
MAX_WAIT_S = 25

#: What a panel may upload. The JSON ceiling is the firmware's result buffer with room to
#: spare; the stream ceiling is the plan's (≤ 1 MB): a 480x480 frame of pure noise is
#: 3 bytes a pixel, ~691 KB, so a real capture never reaches it.
MAX_RESULT_JSON = 64 * 1024
MAX_SHOT_STREAM = 1024 * 1024
#: Generous next to the 480x480 panel; bounds the decoded frame at 3 MB of RGB.
MAX_SHOT_SIDE = 1024


class CommandRefused(ValueError):
    """A command the box will not queue. `reason` is the wire value; `status` the HTTP one."""

    def __init__(self, reason: str, status: int, **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.extra = extra


# device_id -> (monotonic time of its last pickup or upload, the board it said it was).
# In memory for the same reason as `nudge._seen`: the panel re-asserts it every few
# seconds, so a box restart costs one pickup interval of "offline" and nothing else.
_seen: dict[str, tuple[float, str]] = {}
# device_id -> the command list its last `help` answer published.
_published: dict[str, dict[str, CommandInfo]] = {}


def mark_seen(device_id: str, board: str | None, now: float | None = None) -> None:
    """Note a pickup (which names the board) or an upload (None: keep the board known)."""
    if board is None:
        board = _seen.get(device_id, (0.0, ""))[1]
    _seen[device_id] = (time.monotonic() if now is None else now, board)


def last_seen(device_id: str, now: float | None = None) -> tuple[float, str] | None:
    """Seconds since the panel last picked up or answered, and the board it named."""
    seen = _seen.get(device_id)
    if seen is None:
        return None
    t = time.monotonic() if now is None else now
    return max(0.0, t - seen[0]), seen[1]


def record_help(device_id: str, result: dict[str, Any]) -> bool:
    """Keep what a `help` answer published, so later commands are checked against what THIS
    panel's firmware binds (`available`) rather than only the box's copy of the table."""
    data = result.get("data")
    rows = data.get("commands") if isinstance(data, dict) else None
    if result.get("ok") is not True or not isinstance(rows, list):
        return False
    published: dict[str, CommandInfo] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        published[row["name"]] = CommandInfo(
            row["name"],
            bool(row.get("destructive", False)),
            bool(row.get("idempotent", False)),
            bool(row.get("available", True)),
        )
    if not published:
        return False
    _published[device_id] = published
    return True


def published_commands(device_id: str) -> list[CommandInfo] | None:
    pub = _published.get(device_id)
    return list(pub.values()) if pub is not None else None


def forget(device_id: str | None = None) -> None:
    """Drop what is remembered about one panel, or all of them (tests)."""
    if device_id is None:
        _seen.clear()
        _published.clear()
    else:
        _seen.pop(device_id, None)
        _published.pop(device_id, None)


def check_command(device_id: str, name: str) -> CommandInfo:
    """The command as the box will send it, or `CommandRefused`.

    Destructiveness always comes from the box's table: a panel cannot talk the box out of
    the confirm gate by publishing `reboot` as harmless."""
    info = COMMANDS.get(name)
    if info is None:
        raise CommandRefused("unknown_command", 422, commands=sorted(COMMANDS))
    pub = _published.get(device_id)
    if pub is not None:
        theirs = pub.get(name)
        if theirs is None or not theirs.available:
            raise CommandRefused(
                "unavailable", 409, commands=sorted(n for n, c in pub.items() if c.available)
            )
    return info


def check_args(args: dict[str, Any]) -> dict[str, str | int]:
    """The arguments as `dbg_req_parse` will read them: flat, known keys, right types."""
    out: dict[str, str | int] = {}
    for key, value in args.items():
        if key in STR_ARGS:
            if not isinstance(value, str):
                raise CommandRefused("bad_arg", 422, arg=key, want="string")
            if len(value.encode()) > STR_ARGS[key]:
                raise CommandRefused("bad_arg", 422, arg=key, max_len=STR_ARGS[key])
            if any(c in value for c in '"\\') or not value.isprintable():
                # The panel's parser takes `\"` and `\\` but nothing a command needs does,
                # and refusing them keeps the request a byte-for-byte flat object.
                raise CommandRefused("bad_arg", 422, arg=key, want="plain text")
            out[key] = value
        elif key in INT_ARGS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise CommandRefused("bad_arg", 422, arg=key, want="integer")
            if not _INT32[0] <= value <= _INT32[1]:
                raise CommandRefused("bad_arg", 422, arg=key, want="int32")
            out[key] = value
        else:
            raise CommandRefused("bad_arg", 422, arg=key, known=sorted([*STR_ARGS, *INT_ARGS]))
    return out


def panel_request(request_id: str, command: str, args: dict[str, Any], confirm: bool) -> dict:
    """The flat object the panel hands straight to `dbg_cmd_run`."""
    req: dict[str, Any] = {"id": request_id, "cmd": command}
    req.update(args)
    if confirm:
        req["confirm"] = True
    return req


# ---- JPS1 -> PNG -------------------------------------------------------------------------
#
# The stream, as `dbg_shot.c` writes it (all little-endian):
#   "JPS1" u16 w, u16 h
#   per stripe: u16 y0, u16 rows, u32 nbytes, then nbytes of runs (u8 len-1, u16 rgb565)
#   end: u16 0xFFFF
# Stripes arrive top to bottom, full width, and a run never crosses a stripe.


class ShotError(ValueError):
    pass


_R5 = bytes((v * 527 + 23) >> 6 for v in range(32))
_G6 = bytes((v * 259 + 33) >> 6 for v in range(64))


def _rgb(c: int) -> bytes:
    return bytes((_R5[c >> 11], _G6[(c >> 5) & 0x3F], _R5[c & 0x1F]))


@dataclass(frozen=True)
class Shot:
    width: int
    height: int
    stripes: int
    rgb: bytes


def decode_jps1(buf: bytes) -> Shot:
    """Decode a stream into RGB888, refusing anything the panel's own decoder would.

    Same checks as `dbg_shot_decode`, so a torn capture (`net slow` halfway through) is
    an error here rather than a picture with a black bottom half that looks like a fault."""
    if len(buf) < 8 or buf[:4] != b"JPS1":
        raise ShotError("not a JPS1 stream")
    w, h = struct.unpack_from("<HH", buf, 4)
    if not (0 < w <= MAX_SHOT_SIDE and 0 < h <= MAX_SHOT_SIDE):
        raise ShotError(f"size {w}x{h} out of range")
    out = bytearray()
    at, want, stripes = 8, 0, 0
    while True:
        if at + 2 > len(buf):
            raise ShotError("stream ends without its terminator")
        (y0,) = struct.unpack_from("<H", buf, at)
        if y0 == 0xFFFF:
            if want != h:
                raise ShotError(f"stream ends at row {want} of {h}")
            if at + 2 != len(buf):
                raise ShotError("bytes after the terminator")
            return Shot(w, h, stripes, bytes(out))
        if at + 8 > len(buf):
            raise ShotError("stripe header cut short")
        if y0 != want:
            raise ShotError(f"stripe at row {y0}, expected {want}")
        rows, n = struct.unpack_from("<HI", buf, at + 2)
        at += 8
        if not rows or y0 + rows > h or n % 3 or at + n > len(buf):
            raise ShotError(f"bad stripe at row {y0}")
        left = rows * w
        for i in range(at, at + n, 3):
            k = buf[i] + 1
            if k > left:
                raise ShotError(f"run overflows stripe at row {y0}")
            out += _rgb(buf[i + 1] | buf[i + 2] << 8) * k
            left -= k
        if left:
            raise ShotError(f"stripe at row {y0} short by {left} pixels")
        at += n
        want = y0 + rows
        stripes += 1


def png_from_jps1(buf: bytes) -> tuple[bytes, Shot]:
    from PIL import Image  # noqa: PLC0415 - import cost only where an image is drawn

    shot = decode_jps1(buf)
    out = io.BytesIO()
    Image.frombytes("RGB", (shot.width, shot.height), shot.rgb).save(out, "PNG")
    return out.getvalue(), shot
