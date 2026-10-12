"""The debug relay's pure half: the whitelist, the argument checks, and the JPS1 decoder.

The decoder is fed streams built by `jps1()` below, a line-for-line port of
`firmware-c6/components/svc_debug/dbg_shot.c` — the same run limit, the same per-stripe
length header, the same terminator — so what is asserted is what a real capture sends.
"""

from __future__ import annotations

import io
import pathlib
import re
import struct

import pytest
from PIL import Image

from jbrain.jpanel import debug_relay as relay

FIRMWARE = pathlib.Path(__file__).resolve().parents[3] / "firmware-c6"


def jps1(w: int, h: int, frame: list[int], stripe_rows: int = 40) -> bytes:
    """Encode an RGB565 frame exactly as `dbg_shot_begin/stripe/end` would."""
    out = bytearray(b"JPS1" + struct.pack("<HH", w, h))
    for y0 in range(0, h, stripe_rows):
        rows = min(stripe_rows, h - y0)
        px = frame[y0 * w : (y0 + rows) * w]
        runs = bytearray()
        i = 0
        while i < len(px):
            k = 1
            while i + k < len(px) and k < 128 and px[i + k] == px[i]:
                k += 1
            runs += bytes((k - 1,)) + struct.pack("<H", px[i])
            i += k
        out += struct.pack("<HHI", y0, rows, len(runs)) + runs
    out += struct.pack("<H", 0xFFFF)
    return bytes(out)


@pytest.fixture(autouse=True)
def _clean() -> None:
    relay.forget()


# ---- the whitelist ---------------------------------------------------------------------


def test_the_box_s_whitelist_is_the_panel_s() -> None:
    """Spelled twice on purpose (see the module docstring); this is what keeps the copies
    one table. A command the panel adds and the box lacks is refused at the door; one the
    box marks harmless and the panel destructive would skip the box's confirm gate."""
    src = (FIRMWARE / "components" / "svc_debug" / "dbg_cmd.c").read_text(encoding="utf-8")
    table = src[src.index("CMDS[] = {") : src.index("};", src.index("CMDS[] = {"))]
    rows = re.findall(r'\{"(\w+)",\s*(true|false),\s*(true|false)\}', table)
    assert rows, "the firmware stopped declaring CMDS[] the way this test reads it"
    firmware = [(n, d == "true", i == "true") for n, d, i in rows]
    box = [(c.name, c.destructive, c.idempotent) for c in relay.COMMANDS.values()]
    assert box == firmware


def test_the_argument_buffers_are_the_panel_s() -> None:
    """`dbg_req_t`'s char arrays, less the NUL, are the box's string limits."""
    src = (FIRMWARE / "components" / "svc_debug" / "include" / "dbg_core.h").read_text(
        encoding="utf-8"
    )
    for name, limit in relay.STR_ARGS.items():
        m = re.search(rf"char {name}\[(\d+)\];", src)
        assert m, f"dbg_req_t has no {name}"
        assert int(m.group(1)) - 1 == limit
    ints = re.search(r"int32_t ([\w, ]+);", src)
    assert ints and {s.strip() for s in ints.group(1).split(",")} == relay.INT_ARGS


def test_an_unknown_command_is_refused_with_the_list() -> None:
    with pytest.raises(relay.CommandRefused) as exc:
        relay.check_command("dev", "format_flash")
    assert exc.value.reason == "unknown_command" and exc.value.status == 422
    assert "screenshot" in exc.value.extra["commands"]


def test_reboot_is_destructive_and_snapshot_is_not() -> None:
    assert relay.check_command("dev", "reboot").destructive
    assert not relay.check_command("dev", "snapshot").destructive


def _help(rows: list[object]) -> dict:
    return {"id": "x", "cmd": "help", "ok": True, "data": {"commands": rows}}


def test_a_published_help_narrows_what_the_box_will_send() -> None:
    assert relay.record_help(
        "dev",
        _help(
            [
                {"name": "snapshot", "destructive": False, "idempotent": True, "available": True},
                {"name": "inject", "destructive": False, "idempotent": False, "available": False},
                {"name": "reboot", "destructive": False, "idempotent": False, "available": True},
                "junk",
            ]
        ),
    )
    assert relay.check_command("dev", "snapshot").name == "snapshot"
    with pytest.raises(relay.CommandRefused) as exc:
        relay.check_command("dev", "inject")
    assert exc.value.reason == "unavailable" and exc.value.extra["commands"] == [
        "reboot",
        "snapshot",
    ]
    with pytest.raises(relay.CommandRefused):
        relay.check_command("dev", "logs")  # not published at all
    # A panel cannot argue the confirm gate away: destructiveness is the box's.
    assert relay.check_command("dev", "reboot").destructive
    # Another panel is unaffected.
    assert relay.check_command("other", "inject").name == "inject"
    names = [c.name for c in relay.published_commands("dev") or []]
    assert names == ["snapshot", "inject", "reboot"]
    assert relay.published_commands("other") is None


@pytest.mark.parametrize(
    "answer",
    [
        {"ok": False, "err": "x"},
        {"ok": True, "data": {"commands": "nope"}},
        {"ok": True, "data": []},
        {"ok": True, "data": {"commands": [{"nameless": 1}]}},
    ],
)
def test_a_help_that_says_nothing_usable_is_not_recorded(answer: dict) -> None:
    assert not relay.record_help("dev", answer)
    assert relay.published_commands("dev") is None


def test_forget_one_panel_keeps_the_others() -> None:
    relay.mark_seen("a", "c6-amoled-216", now=1.0)
    relay.mark_seen("b", "c6-amoled-216", now=1.0)
    relay.forget("a")
    assert relay.last_seen("a") is None
    assert relay.last_seen("b", now=4.0) == (3.0, "c6-amoled-216")


def test_last_seen_never_runs_backwards() -> None:
    relay.mark_seen("a", "c6-amoled-216", now=10.0)
    assert relay.last_seen("a", now=5.0) == (0.0, "c6-amoled-216")
    relay.mark_seen("a", "s3-amoled-18")
    seen = relay.last_seen("a")
    assert seen is not None and seen[1] == "s3-amoled-18" and seen[0] < 1.0


# ---- arguments -------------------------------------------------------------------------


def test_good_arguments_pass_through() -> None:
    args = {"event": "touch", "x": 240, "y": 0, "value": -60, "app": "clock", "name": "bright"}
    assert relay.check_args(args) == args


@pytest.mark.parametrize(
    ("args", "detail"),
    [
        ({"event": 3}, {"arg": "event", "want": "string"}),
        ({"event": "x" * 12}, {"arg": "event", "max_len": 11}),
        ({"app": 'a"b'}, {"arg": "app", "want": "plain text"}),
        ({"app": "a\\b"}, {"arg": "app", "want": "plain text"}),
        ({"tag": "a\nb"}, {"arg": "tag", "want": "plain text"}),
        ({"x": "1"}, {"arg": "x", "want": "integer"}),
        ({"x": True}, {"arg": "x", "want": "integer"}),
        ({"x": 1.5}, {"arg": "x", "want": "integer"}),
        ({"value": 2**31}, {"arg": "value", "want": "int32"}),
        ({"level": -(2**31) - 1}, {"arg": "level", "want": "int32"}),
    ],
)
def test_bad_arguments_are_refused_by_name(args: dict, detail: dict) -> None:
    with pytest.raises(relay.CommandRefused) as exc:
        relay.check_args(args)
    assert exc.value.reason == "bad_arg" and exc.value.status == 422
    assert exc.value.extra == detail


def test_an_unknown_argument_names_the_known_ones() -> None:
    """The panel would skip it silently; the box says so instead of letting a typo pass as
    a command that ran with no argument."""
    with pytest.raises(relay.CommandRefused) as exc:
        relay.check_args({"evnet": "touch"})
    assert exc.value.extra["arg"] == "evnet"
    assert "event" in exc.value.extra["known"]


def test_the_request_is_flat_and_confirm_only_when_true() -> None:
    assert relay.panel_request("r1", "snapshot", {}, False) == {"id": "r1", "cmd": "snapshot"}
    assert relay.panel_request("r2", "reboot", {}, True) == {
        "id": "r2",
        "cmd": "reboot",
        "confirm": True,
    }
    req = relay.panel_request("r3", "inject", {"event": "touch", "x": 1, "y": 2}, False)
    assert all(not isinstance(v, (dict, list)) for v in req.values())


# ---- JPS1 ------------------------------------------------------------------------------


def _frame(w: int, h: int) -> list[int]:
    """A background with a bar and some noise: long runs, a run limit crossing, and
    single-pixel runs, so every encoder branch is in the stream."""
    frame = [0x0000] * (w * h)
    for y in range(h):
        for x in range(w):
            if 10 <= y < 20:
                frame[y * w + x] = 0xF800  # red bar
            elif y == 30:
                frame[y * w + x] = (x * 2654435761) & 0xFFFF  # noise
            elif x < 5:
                frame[y * w + x] = 0x07E0  # green edge
    return frame


def test_a_stream_decodes_to_the_frame_it_encoded() -> None:
    w, h = 200, 90  # 200 px rows cross the 128-pixel run limit; 90 rows end on a short stripe
    stream = jps1(w, h, _frame(w, h))
    shot = relay.decode_jps1(stream)
    assert (shot.width, shot.height, shot.stripes) == (w, h, 3)
    assert len(shot.rgb) == w * h * 3
    px = lambda x, y: shot.rgb[(y * w + x) * 3 : (y * w + x) * 3 + 3]  # noqa: E731
    assert px(50, 15) == b"\xff\x00\x00"
    assert px(2, 50) == b"\x00\xff\x00"
    assert px(150, 60) == b"\x00\x00\x00"


def test_rgb565_white_and_mid_tones_expand_correctly() -> None:
    stream = jps1(2, 1, [0xFFFF, 0x8410])
    assert relay.decode_jps1(stream).rgb == b"\xff\xff\xff\x84\x82\x84"


def test_the_png_is_a_real_png_of_that_frame() -> None:
    w, h = 48, 40
    png, shot = relay.png_from_jps1(jps1(w, h, _frame(w, h), stripe_rows=8))
    assert shot.stripes == 5
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size == (w, h) and img.mode == "RGB"
    assert img.getpixel((10, 12)) == (255, 0, 0)


def test_the_full_panel_screen_decodes() -> None:
    """480x480 in 480x40 strips: the real geometry."""
    frame = [0x001F] * (480 * 480)
    shot = relay.decode_jps1(jps1(480, 480, frame))
    assert shot.stripes == 12 and len(shot.rgb) == 480 * 480 * 3


def _bad_streams() -> list[tuple[bytes, str]]:
    good = jps1(4, 2, [1, 1, 2, 2, 3, 3, 4, 4], stripe_rows=1)
    return [
        (b"", "not a JPS1"),
        (b"JPS2" + good[4:], "not a JPS1"),
        (b"JPS1" + struct.pack("<HH", 0, 2), "out of range"),
        (b"JPS1" + struct.pack("<HH", 4, 2000), "out of range"),
        (good[:-2], "terminator"),
        (good + b"\x00", "after the terminator"),
        (good[:12], "cut short"),
        # Torn mid-capture (`net slow`): the first stripe, then the terminator.
        (good[: 8 + 8 + 6] + struct.pack("<H", 0xFFFF), "ends at row 1 of 2"),
        # Second stripe claims row 0 again.
        (good[:22] + struct.pack("<H", 0) + good[24:], "expected 1"),
        # A stripe claiming more rows than the frame has.
        (good[:22] + struct.pack("<HHI", 1, 5, 6) + good[30:], "bad stripe"),
        # Length not a whole number of runs.
        (good[:8] + struct.pack("<HHI", 0, 1, 5) + good[16:21] + good[22:], "bad stripe"),
        # A run longer than its stripe.
        (good[:16] + b"\x09" + good[17:], "overflows"),
        # Runs that leave the stripe short.
        (
            good[:8] + struct.pack("<HHI", 0, 1, 3) + b"\x00\x01\x00" + good[22:],
            "short by 3",
        ),
    ]


@pytest.mark.parametrize(("stream", "why"), _bad_streams())
def test_a_bad_stream_is_refused_not_drawn(stream: bytes, why: str) -> None:
    with pytest.raises(relay.ShotError, match=why):
        relay.decode_jps1(stream)
