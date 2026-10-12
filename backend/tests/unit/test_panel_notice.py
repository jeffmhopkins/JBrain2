"""The notice path's pure half: limits, the refusal table, the picture, the sound, JN1.

The picture's runs are checked byte for byte against `jps1()` — the line-for-line port of the
panel's own screenshot encoder in `test_panel_debug_relay.py` — so the stripes the box sends
are the runs the panel already knows how to read. The `JN1` streams in
`tests/fixtures/jpanel_notice/` are the firmware wave's test vectors: this file rebuilds each
from its description and requires the bytes on disk to match (set `JPANEL_NOTICE_REGEN=1` to
rewrite them after a deliberate format change).
"""

from __future__ import annotations

import array
import hashlib
import io
import json
import math
import os
import pathlib
import re
import struct
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from jbrain.jpanel import notice as N
from tests.unit.test_panel_debug_relay import jps1

FIRMWARE = pathlib.Path(__file__).resolve().parents[3] / "firmware-c6"
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "jpanel_notice"


@pytest.fixture(autouse=True)
def _clean() -> None:
    N.forget_prefs()


def refused(fn, *args, **kw) -> N.NoticeRefused:  # noqa: ANN001
    with pytest.raises(N.NoticeRefused) as info:
        fn(*args, **kw)
    return info.value


def png(img: Image.Image, fmt: str = "PNG") -> bytes:
    out = io.BytesIO()
    img.save(out, fmt)
    return out.getvalue()


# ---- the text --------------------------------------------------------------------------


def test_text_within_the_limits_passes_trimmed() -> None:
    assert N.check_text("  Disk 91% full ", "Ops → Disk", ["Yes", "No"]) == (
        "Disk 91% full",
        "Ops → Disk",
        ["Yes", "No"],
    )


@pytest.mark.parametrize(
    ("title", "body", "choices", "reason", "field"),
    [
        ("", "", [], "bad_field", "title"),
        ("x" * 61, "", [], "too_long", "title"),
        ("two\nlines", "", [], "bad_field", "title"),
        ("ok", "y" * 301, [], "too_long", "body"),
        ("ok", "bell\x07", [], "bad_field", "body"),
        ("ok", "", ["a", "b", "c", "d", "e"], "too_many_choices", None),
        ("ok", "", ["x" * 17], "too_long", "choices"),
        ("ok", "", [""], "bad_field", "choices"),
        ("ok", "", ["a\nb"], "bad_field", "choices"),
        ("ok", "", ["Yes", "Yes"], "bad_field", "choices"),
    ],
)
def test_text_the_panel_cannot_show_is_refused(
    title: str, body: str, choices: list[str], reason: str, field: str | None
) -> None:
    exc = refused(N.check_text, title, body, choices)
    assert (exc.reason, exc.status) == (reason, 422)
    assert exc.extra.get("field") == field


def test_limits_are_bytes_not_characters() -> None:
    assert N.check_text("é" * 30, "", [])[0] == "é" * 30
    assert refused(N.check_text, "é" * 31, "", []).reason == "too_long"
    assert refused(N.check_text, "ok", "", ["ü" * 9]).reason == "too_long"


def test_a_body_may_hold_line_breaks() -> None:
    assert N.check_text("t", "one\ntwo", [])[1] == "one\ntwo"


def test_hide_and_ttl_bounds() -> None:
    assert N.check_hide(None) is None
    assert N.check_hide(3) == 3 and N.check_hide(600) == 600
    for bad in (2, 601, 0, -1):
        assert refused(N.check_hide, bad).extra["field"] == "hide_after_s"
    assert N.check_ttl(None) == N.TTL_DEFAULT_S == 900
    assert N.check_ttl(30) == 30 and N.check_ttl(86400) == 86400
    for bad in (29, 86401):
        assert refused(N.check_ttl, bad).extra["field"] == "ttl_s"


def test_senders_and_how_the_panel_names_them() -> None:
    for ok in ("jerv", "ops", "owner", "debug", "workflow:nightly_backup", "workflow:a.b-c"):
        assert N.check_sender(ok) == ok
    for bad in ("Jerv", "workflow:", "workflow:Bad", "someone", "workflow:" + "a" * 49):
        assert refused(N.check_sender, bad).reason == "bad_sender"
    assert N.sender_label("jerv") == "Jerv"
    assert N.sender_label("ops") == "Ops"
    assert N.sender_label("owner") == "You"
    assert N.sender_label("debug") == "Debug"
    assert N.sender_label("workflow:nightly_backup") == "Nightly backup"
    assert len(N.sender_label("workflow:" + "a" * 48).encode()) == N.SENDER_LABEL_BYTES


# ---- what the panel reports -------------------------------------------------------------

NIGHT = {"na": "1", "nq": "1", "nc": "1", "nf": "1290", "nt": "390", "tz": "5"}


def test_prefs_parse_from_the_pickup_query() -> None:
    prefs = N.parse_prefs(NIGHT)
    assert prefs == N.NoticePrefs(True, True, True, 1290, 390, 5)
    assert prefs is not None and prefs.as_dict() == {
        "accept": True,
        "quiet": True,
        "chime": True,
        "night_from": "21:30",
        "night_to": "06:30",
        "tz": "America/New_York",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"na": None},
        {"tz": None},
        {"na": "2"},
        {"nq": "yes"},
        {"nf": "1440"},
        {"nt": "-1"},
        {"tz": "12"},
        {"tz": "-1"},
    ],
)
def test_a_half_understood_report_is_no_report(change: dict[str, str | None]) -> None:
    params = {k: v for k, v in {**NIGHT, **change}.items() if v is not None}
    assert N.parse_prefs(params) is None


def test_prefs_are_remembered_per_panel_and_cleared_by_a_pickup_without_them() -> None:
    prefs = N.parse_prefs(NIGHT)
    N.record_prefs("a", prefs)
    assert N.prefs_for("a") == prefs and N.prefs_for("b") is None
    N.record_prefs("a", None)
    assert N.prefs_for("a") is None
    N.record_prefs("a", prefs)
    N.forget_prefs("a")
    assert N.prefs_for("a") is None


def test_the_zone_table_is_the_panel_s() -> None:
    """Spelled twice on purpose, like the debug whitelist: the panel reports an index into its
    table, so the box's copy must be the same zones in the same order."""
    src = (FIRMWARE / "components/svc_rtc/time_logic.c").read_text()
    table = re.search(r"ZONES\[TZ_ZONES\] = \{(.*?)\n\};", src, re.S)
    assert table is not None
    rows = re.findall(r'\{"([^"]+)", "([^"]+)"\}', table.group(1))
    assert [label for label, _ in rows] == [label for label, _ in N.PANEL_ZONES]
    zones = re.search(
        r"#define TZ_ZONES (\d+)", (FIRMWARE / "components/app_api/include/svc_rtc.h").read_text()
    )
    assert zones is not None and int(zones.group(1)) == len(N.PANEL_ZONES)
    for (_, posix), (_, iana) in zip(rows, N.PANEL_ZONES, strict=True):
        m = re.match(r"[A-Z]+(-?\d+)", posix)
        assert m is not None
        std = timedelta(hours=-int(m.group(1)))
        zone = ZoneInfo(iana)
        offsets = [datetime(2026, mo, 15, 12, tzinfo=zone).utcoffset() for mo in (1, 7)]
        assert min(o for o in offsets if o is not None) == std, iana


# ---- the refusal table -------------------------------------------------------------------

NY = ZoneInfo("America/New_York")


def at(h: int, m: int, day: int = 12, month: int = 10) -> datetime:
    return datetime(2026, month, day, h, m, tzinfo=NY).astimezone(UTC)


def test_in_window_wraps_midnight_and_an_empty_window_is_never() -> None:
    assert N.in_window(22 * 60, 1290, 390) and N.in_window(0, 1290, 390)
    assert N.in_window(389, 1290, 390) and not N.in_window(390, 1290, 390)
    assert not N.in_window(12 * 60, 1290, 390)
    assert N.in_window(600, 540, 1020) and not N.in_window(1020, 540, 1020)
    assert not any(N.in_window(m, 300, 300) for m in range(0, 1440, 7))


def test_quiet_hours_end_at_the_panel_s_own_morning() -> None:
    prefs = N.NoticePrefs(True, True, False, 1290, 390, 5)
    assert N.quiet_until(prefs, at(12, 0)) is None
    assert N.quiet_until(prefs, at(23, 0)) == at(6, 30, day=13)
    assert N.quiet_until(prefs, at(2, 0)) == at(6, 30)
    off = N.NoticePrefs(True, False, False, 1290, 390, 5)
    assert N.quiet_until(off, at(23, 0)) is None


def test_quiet_hours_across_a_clock_change_end_on_the_wall_clock() -> None:
    # US DST ended 2026-11-01 at 02:00: the night of Oct 31 is an hour longer, and the window
    # still ends at 06:30 New York time, not 05:30.
    prefs = N.NoticePrefs(True, True, False, 1290, 390, 5)
    until = N.quiet_until(prefs, at(23, 0, day=31))
    assert until is not None
    assert until.astimezone(NY).strftime("%m-%d %H:%M") == "11-01 06:30"
    assert until == datetime(2026, 11, 1, 11, 30, tzinfo=UTC)


def test_the_window_is_in_the_panel_s_zone_not_the_box_s() -> None:
    london = N.NoticePrefs(True, True, False, 1290, 390, 8)
    # 23:00 in New York is 04:00 in London: inside London's night.
    until = N.quiet_until(london, at(23, 0))
    assert until is not None and until.astimezone(ZoneInfo("Europe/London")).hour == 6


def test_the_refusal_table_in_order() -> None:
    noon, night = at(12, 0), at(23, 0)
    prefs = N.NoticePrefs(True, True, True, 1290, 390, 5)

    off = N.refusal(None, prefs, noon)
    assert off is not None and (off.reason, off.status, off.extra) == (
        "offline",
        503,
        {"last_seen_s": None},
    )
    stale = N.refusal((31.0, "c6-amoled-216"), prefs, noon)
    assert stale is not None and stale.extra == {"last_seen_s": 31}
    assert N.refusal((30.0, "c6-amoled-216"), prefs, noon) is None

    old = N.refusal((1.0, "c6-amoled-216"), None, noon)
    assert old is not None and (old.reason, old.status) == ("no_notice_surface", 409)
    assert old.extra == {"board": "c6-amoled-216"}

    no = N.refusal((1.0, "c6"), N.NoticePrefs(False, True, True, 1290, 390, 5), noon)
    assert no is not None and (no.reason, no.status) == ("not_accepting", 409)

    quiet = N.refusal((1.0, "c6"), prefs, night)
    assert quiet is not None and (quiet.reason, quiet.status) == ("quiet_hours", 409)
    assert quiet.extra == {
        "until": "06:30",
        "until_at": "2026-10-13T10:30:00+00:00",
        "tz": "America/New_York",
    }
    assert quiet.detail()["reason"] == "quiet_hours"

    loud = N.refusal((1.0, "c6"), N.NoticePrefs(True, False, True, 1290, 390, 5), night)
    assert loud is None
    assert N.refusal((1.0, "c6"), prefs, noon) is None


def test_offline_wins_over_every_switch() -> None:
    switched_off = N.NoticePrefs(False, True, True, 1290, 390, 5)
    out = N.refusal((90.0, "c6"), switched_off, at(23, 0))
    assert out is not None and out.reason == "offline"


# ---- the picture -------------------------------------------------------------------------


def frame_of(img: Image.Image) -> list[int]:
    return N.to_rgb565(img.convert("RGB"))


def pixels_of(stripes: tuple[bytes, ...], width: int) -> list[int]:
    out: list[int] = []
    for s in stripes:
        for i in range(4, len(s), 3):
            out.extend([s[i + 1] | s[i + 2] << 8] * (s[i] + 1))
    return out


def test_runs_are_the_screenshot_encoder_s_runs() -> None:
    """Each STRIPE's runs are exactly what `dbg_shot.c` emits for the same rows, so the
    panel's run reader serves both directions."""
    w, h = 480, 120
    frame = [((x // 7) * 31 + (y // 3) * 7) & 0xFFFF for y in range(h) for x in range(w)]
    stripes = N.stripes(frame, w, h)
    for s in stripes:
        y0, rows = struct.unpack_from("<HH", s)
        shot = jps1(w, rows, frame[y0 * w : (y0 + rows) * w], stripe_rows=rows)
        # JPS1: magic, w, h, then one stripe header (y0, rows, nbytes) and its runs.
        (nbytes,) = struct.unpack_from("<I", shot, 12)
        assert s[4:] == shot[16 : 16 + nbytes]
        assert N.runs(frame[y0 * w : (y0 + rows) * w]) == s[4:]
    assert pixels_of(stripes, w) == frame


def test_stripes_are_whole_rows_contiguous_and_fit_one_record() -> None:
    w, h = 480, 480
    noise = [(i * 2654435761 >> 7) & 0xFFFF for i in range(w * h)]
    stripes = N.stripes(noise, w, h)
    want = 0
    for s in stripes:
        y0, rows = struct.unpack_from("<HH", s)
        assert y0 == want and 1 <= rows <= N.STRIPE_ROWS_MAX
        assert len(s) <= N.MAX_RECORD
        want = y0 + rows
    assert want == h
    # Noise is three bytes a pixel: two rows (2880 B) fit a record, three (4320 B) do not.
    assert {struct.unpack_from("<HH", s)[1] for s in stripes} == {2}
    flat = N.stripes([0x1234] * (w * h), w, h)
    assert [struct.unpack_from("<HH", s)[1] for s in flat] == [40] * 12


def test_a_run_never_passes_128() -> None:
    s = N.stripes([7] * 480, 480, 1)[0]
    assert [s[i] + 1 for i in range(4, len(s), 3)] == [128, 128, 128, 96]


def test_full_mode_letterboxes_on_black_to_the_whole_glass() -> None:
    wide = Image.new("RGB", (960, 480), (255, 0, 0))
    f = N.fit_image(png(wide), "full")
    assert (f.mode, f.width, f.height, f.posterize) == ("full", 480, 480, 0)
    px = pixels_of(f.stripes, 480)
    assert len(px) == 480 * 480
    assert px[0] == 0 and px[240 * 480] == 0xF800 and px[-1] == 0


def test_banner_mode_keeps_the_shape_up_to_half_the_glass() -> None:
    f = N.fit_image(png(Image.new("RGB", (960, 200), (0, 255, 0))), "banner")
    assert (f.width, f.height) == (480, 100)
    tall = N.fit_image(png(Image.new("RGB", (300, 900), (0, 0, 255))), "banner")
    assert (tall.width, tall.height) == (480, N.BANNER_MAX_H)
    px = pixels_of(tall.stripes, 480)
    assert px[0] == 0 and px[240] == 0x001F


def test_a_banner_drawn_to_size_passes_through_pixel_for_pixel() -> None:
    img = Image.new("RGB", (480, 64))
    img.putdata(
        [((x * 3) % 256, (y * 4) % 256, (x ^ y) % 256) for y in range(64) for x in range(480)]
    )
    f = N.fit_image(png(img), "banner")
    assert (f.width, f.height) == (480, 64)
    assert pixels_of(f.stripes, 480) == frame_of(img)


def test_transparent_is_unlit() -> None:
    img = Image.new("RGBA", (480, 10), (255, 255, 255, 0))
    img.putpixel((0, 0), (255, 255, 255, 255))
    px = pixels_of(N.fit_image(png(img), "banner").stripes, 480)
    assert px[0] == 0xFFFF and set(px[1:]) == {0}


def test_jpeg_and_webp_are_taken_and_exif_rotation_is_honoured() -> None:
    assert N.fit_image(png(Image.new("RGB", (64, 32), "white"), "JPEG"), "banner").height == 240
    assert N.fit_image(png(Image.new("RGB", (64, 32), "white"), "WEBP"), "banner").height == 240
    img = Image.new("RGB", (64, 32), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90: the picture is really 32 wide, 64 tall
    out = io.BytesIO()
    img.save(out, "JPEG", exif=exif.tobytes())
    assert N.fit_image(out.getvalue(), "banner").height == N.BANNER_MAX_H


def test_pictures_the_panel_cannot_take_are_refused() -> None:
    gif = refused(N.fit_image, png(Image.new("RGB", (8, 8)), "GIF"), "full")
    assert (gif.reason, gif.status) == ("image_format", 415)
    junk = refused(N.fit_image, b"\x89PNG\r\n\x1a\nnot really", "full")
    assert (junk.reason, junk.status) == ("image_unreadable", 422)
    big = refused(N.fit_image, b"\0" * (N.IMAGE_UPLOAD_MAX + 1), "full")
    assert (big.reason, big.status) == ("image_too_big", 413)


def test_a_bomb_is_refused_before_it_is_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(N, "IMAGE_PIXELS_MAX", 100)
    exc = refused(N.fit_image, png(Image.new("RGB", (20, 20))), "full")
    assert (exc.reason, exc.extra) == ("image_too_big", {"max_pixels": 100})


def test_a_photograph_is_posterized_to_fit_and_noise_is_refused() -> None:
    w, h = 480, 480
    smooth = Image.radial_gradient("L").resize((w, h)).convert("RGB")
    grain = Image.effect_noise((w, h), 12).convert("RGB")
    photo = Image.blend(smooth, grain, 0.25)
    f = N.fit_image(png(photo), "full")
    assert f.posterize in (4, 3) and f.stream_bytes <= N.IMAGE_STREAM_MAX
    noise = Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))
    exc = refused(N.fit_image, png(noise), "full")
    assert (exc.reason, exc.status) == ("image_too_complex", 413)
    assert exc.extra["max_bytes"] == N.IMAGE_STREAM_MAX and exc.extra["bytes"] > N.IMAGE_STREAM_MAX


def test_the_default_mode() -> None:
    assert N.default_mode("", []) == "full"
    assert N.default_mode("words", []) == "banner"
    assert N.default_mode("", ["Yes"]) == "banner"


def test_blob_is_the_stripe_records_as_sent() -> None:
    f = N.fit_image(png(Image.new("RGB", (480, 50), "red")), "banner")
    assert f.blob() == b"".join(N.record(N.STRIPE, s) for s in f.stripes)
    assert f.stream_bytes == len(f.blob())


# ---- the sound ---------------------------------------------------------------------------


def wav(pcm: bytes, rate: int = 16000, channels: int = 1, bits: int = 16, tag: int = 1) -> bytes:
    block = channels * bits // 8
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits)
    # A LIST chunk first, as most editors write: the reader must walk, not skip 44 bytes.
    junk = b"LIST" + struct.pack("<I", 5) + b"abcde\0"
    return (
        b"RIFF"
        + struct.pack("<I", 4 + len(junk) + 8 + len(fmt) + 8 + len(pcm))
        + b"WAVE"
        + junk
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def tone(ms: int, rate: int = 16000, hz: float = 440.0) -> bytes:
    n = rate * ms // 1000
    return array.array(
        "h", (int(8000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(n))
    ).tobytes()


def test_a_panel_rate_wav_passes_straight_through() -> None:
    pcm = tone(500)
    assert N.wav_pcm(wav(pcm)) == pcm


def test_a_stereo_48k_wav_is_mixed_down_and_resampled() -> None:
    left = array.array("h", [1000, -1000] * 24000)
    right = array.array("h", [3000, 1000] * 24000)
    inter = array.array("h", (v for pair in zip(left, right, strict=True) for v in pair))
    out = N.wav_pcm(wav(inter.tobytes(), rate=48000, channels=2))
    assert out is not None
    assert abs(N.pcm_ms(out) - 1000) <= 1
    first = array.array("h")
    first.frombytes(out[:2])
    assert first[0] == 2000


@pytest.mark.parametrize(
    "data",
    [
        b"not a wav at all",
        wav(b"\0" * 64, bits=8),
        wav(b"\0" * 64, tag=3),
        wav(b"\0" * 64, channels=6),
        b"RIFF\x04\0\0\0WAVE",
    ],
)
def test_a_wav_this_does_not_read_is_handed_on(data: bytes) -> None:
    assert N.wav_pcm(data) is None


def test_the_clip_is_capped_at_eight_seconds() -> None:
    assert N.check_pcm(tone(8000)) == tone(8000)
    exc = refused(N.check_pcm, tone(8001))
    assert (exc.reason, exc.status, exc.extra) == (
        "audio_too_long",
        422,
        {"ms": 8001, "max_ms": 8000},
    )
    assert refused(N.check_pcm, b"\0").reason == "audio_empty"
    assert N.check_pcm(b"\1\2\3") == b"\1\2"


def test_resample_leaves_the_panel_rate_alone() -> None:
    assert N.resample(b"\1\2", 16000) == b"\1\2"
    assert N.resample(b"", 8000) == b""
    assert N.pcm_ms(N.resample(tone(1000, rate=8000), 8000)) == 999


# ---- JN1 -----------------------------------------------------------------------------------

ID = "6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def notice(**kw) -> N.WireNotice:  # noqa: ANN003
    base = {
        "id": ID,
        "sender_label": "Jerv",
        "title": "Run the cleanup now?",
        "body": "Frees about 38 GB of build cache.",
        "choices": ("Yes", "No"),
        "chime": True,
        "hide_after_s": None,
        "ttl_s": 900,
    }
    base.update(kw)
    return N.WireNotice(**base)


def test_round_trip_with_everything() -> None:
    f = N.fit_image(png(Image.new("RGB", (480, 80), (40, 90, 200))), "banner")
    pcm = tone(250)
    n = notice(
        image_mode="banner", image_w=480, image_h=80, audio_ms=N.pcm_ms(pcm), hide_after_s=20
    )
    d = N.decode(N.encode(n, f.blob(), pcm))
    assert d.notice == n and d.pcm == pcm and d.stripes == len(f.stripes)
    assert d.pixels == tuple(pixels_of(f.stripes, 480))


def test_a_plain_notice_has_no_body_record_when_empty_and_no_flags() -> None:
    n = notice(body="", choices=(), chime=False)
    stream = N.encode(n)
    assert stream[:3] == b"JN1"
    kinds = []
    at = 3
    while at < len(stream):
        kind, size = struct.unpack_from("<BH", stream, at)
        kinds.append(kind)
        at += 3 + size
    assert kinds == [N.META, N.TITLE, N.END]
    assert stream[3 + 3 + 16] == 0  # flags
    assert N.decode(stream).notice == n


def test_meta_layout_is_fixed() -> None:
    n = notice(
        hide_after_s=45, ttl_s=0x01020304, image_mode="full", image_w=480, image_h=480, audio_ms=0
    )
    f = N.FittedImage("full", 480, 480, N.stripes([0] * (480 * 480), 480, 480), 0)
    stream = N.encode(n, f.blob())
    kind, size = struct.unpack_from("<BH", stream, 3)
    meta = stream[6 : 6 + size]
    assert kind == N.META and size == 30 + len(b"Jerv")
    assert meta[:16] == uuid.UUID(ID).bytes
    assert meta[16] == N.FLAG_CHIME | N.FLAG_QUESTION | N.FLAG_PICTURE
    assert struct.unpack_from("<HIBHHH", meta, 17) == (45, 0x01020304, N.IMG_FULL, 480, 480, 0)
    assert meta[30:] == b"Jerv"


def test_end_counts_the_bytes_before_it() -> None:
    stream = N.encode(notice())
    kind, size = struct.unpack_from("<BH", stream, len(stream) - 8)
    assert (kind, size) == (N.END, 5)
    assert struct.unpack_from("<BI", stream, len(stream) - 5) == (0, len(stream) - 8)


def test_long_sound_is_cut_into_records() -> None:
    pcm = tone(1000)
    d = N.decode(N.encode(notice(audio_ms=1000), pcm=pcm))
    assert d.pcm == pcm
    assert N.encode(notice(audio_ms=1000), pcm=pcm).count(bytes([N.PCM])) >= 8


def test_an_oversized_record_is_never_built() -> None:
    with pytest.raises(ValueError, match="record of 4097"):
        N.record(N.BODY, b"x" * 4097)


def _corrupt(stream: bytes, at: int, data: bytes) -> bytes:
    return stream[:at] + data + stream[at + len(data) :]


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (lambda s: b"JPS1" + s[3:], "not a JN1"),
        (lambda s: s[:-8], "without END"),
        (lambda s: s + b"\0", "after END"),
        (lambda s: s[:-4] + struct.pack("<I", 7), "END does not match"),
        (lambda s: s[:-5] + b"\1" + s[-4:], "END does not match"),
        (lambda s: s[:3] + N.record(N.TITLE, b"x") + s[3:], "not META"),
        (lambda s: s[:-8] + N.record(N.META, b"\0" * 30) + s[-8:], "out of order"),
        (lambda s: s[:-8] + N.record(N.PCM, b"\0" * 3) + s[-8:], "odd PCM"),
        (lambda s: s[:3] + N.record(N.META, b"\0" * 12), "META too short"),
        (lambda s: s[:-2], "cut short"),
    ],
)
def test_the_reader_refuses_a_torn_or_garbled_stream(make, why: str) -> None:  # noqa: ANN001
    stream = N.encode(notice())
    with pytest.raises(N.WireError, match=why):
        N.decode(make(stream))


def test_the_reader_holds_the_flags_to_the_records() -> None:
    no_choices = N.encode(notice(choices=()))
    flagged = _corrupt(no_choices, 6 + 16, bytes([N.FLAG_QUESTION]))
    with pytest.raises(N.WireError, match="question flag"):
        N.decode(_fix_end(flagged))
    with_pic = N.encode(
        notice(image_mode="banner", image_w=4, image_h=2),
        N.record(N.STRIPE, struct.pack("<HH", 0, 1) + struct.pack("<BH", 3, 9)),
    )
    with pytest.raises(N.WireError, match="picture flag"):
        N.decode(with_pic)  # one row of two
    sound = N.encode(notice(audio_ms=900), pcm=tone(250))
    with pytest.raises(N.WireError, match="sound flag"):
        N.decode(sound)


def _fix_end(stream: bytes) -> bytes:
    return stream[:-4] + struct.pack("<I", len(stream) - 8)


@pytest.mark.parametrize(
    ("stripe", "why"),
    [
        (struct.pack("<HH", 1, 1) + struct.pack("<BH", 3, 0), "stripe at row 1"),
        (struct.pack("<HH", 0, 1) + struct.pack("<BH", 4, 0), "overflows"),
        (struct.pack("<HH", 0, 1) + struct.pack("<BH", 2, 0), "short by 1"),
        (struct.pack("<HH", 0, 1) + struct.pack("<BH", 3, 0) + b"\0", "overflows"),
        (struct.pack("<HH", 0, 41) + struct.pack("<BH", 3, 0), "stripe at row 0"),
    ],
)
def test_the_reader_refuses_a_bad_stripe(stripe: bytes, why: str) -> None:
    n = notice(image_mode="banner", image_w=4, image_h=1)
    with pytest.raises(N.WireError, match=why):
        N.decode(N.encode(n, N.record(N.STRIPE, stripe)))


def test_an_unknown_record_is_skipped_by_its_length() -> None:
    stream = N.encode(notice())
    extra = stream[:-8] + N.record(0x7E, b"future") + stream[-8:]
    assert N.decode(_fix_end(extra)).notice == notice()


# ---- the fixtures: the firmware wave's test vectors ----------------------------------------


def _vectors() -> dict[str, tuple[N.WireNotice, bytes, bytes]]:
    w = 480
    # A drawn banner: three flat bands and a two-pixel rule, the shape a sender's banner has.
    banner_px = [
        0x7D39 if y < 24 else (0xFFFF if y < 26 else (0x0000 if x < 240 else 0xC514))
        for y in range(48)
        for x in range(w)
    ]
    # A full-screen picture: a 16 px checkerboard — many runs, still small.
    full_px = [
        0xF800 if ((x // 16) ^ (y // 16)) & 1 else 0x001F for y in range(480) for x in range(w)
    ]
    full = N.FittedImage("full", w, 480, N.stripes(full_px, w, 480), 0)
    banner = N.FittedImage("banner", w, 48, N.stripes(banner_px, w, 48), 0)
    chirp = tone(250, hz=880.0)
    return {
        "plain": (
            N.WireNotice(
                ID, "Ops", "Disk 91% full", "The box is running low on space.", (), False, None, 900
            ),
            b"",
            b"",
        ),
        "question": (
            N.WireNotice(
                ID,
                "Jerv",
                "Run the cleanup now?",
                "Frees about 38 GB.",
                ("Yes", "No"),
                True,
                30,
                900,
            ),
            b"",
            b"",
        ),
        "banner": (
            N.WireNotice(
                ID,
                "Nightly backup",
                "Backup done",
                "",
                ("OK", "Show me", "Later"),
                False,
                None,
                600,
                "banner",
                w,
                48,
            ),
            banner.blob(),
            b"",
        ),
        "full_sound": (
            N.WireNotice(ID, "Jerv", "Look", "", (), True, 8, 120, "full", w, 480, N.pcm_ms(chirp)),
            full.blob(),
            chirp,
        ),
    }


def test_the_fixtures_are_what_the_encoder_writes() -> None:
    regen = os.environ.get("JPANEL_NOTICE_REGEN") == "1"
    manifest: dict[str, dict[str, object]] = {}
    for name, (n, image, pcm) in _vectors().items():
        stream = N.encode(n, image, pcm)
        path = FIXTURES / f"{name}.jn1"
        if regen:
            FIXTURES.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stream)
        assert path.read_bytes() == stream, f"{path.name} differs from the encoder"
        d = N.decode(stream)
        assert d.notice == n
        manifest[name] = {
            "bytes": len(stream),
            "sha256": hashlib.sha256(stream).hexdigest(),
            "id": n.id,
            "sender": n.sender_label,
            "title": n.title,
            "body": n.body,
            "choices": list(n.choices),
            "chime": n.chime,
            "hide_after_s": n.hide_after_s,
            "ttl_s": n.ttl_s,
            "image": (
                {
                    "mode": n.image_mode,
                    "w": n.image_w,
                    "h": n.image_h,
                    "stripes": d.stripes,
                    "rgb565_le_sha256": hashlib.sha256(
                        struct.pack(f"<{len(d.pixels)}H", *d.pixels)
                    ).hexdigest(),
                }
                if image
                else None
            ),
            "audio": (
                {"ms": n.audio_ms, "pcm_sha256": hashlib.sha256(d.pcm).hexdigest()} if pcm else None
            ),
        }
    mpath = FIXTURES / "vectors.json"
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if regen:
        mpath.write_text(text)
    assert json.loads(mpath.read_text()) == manifest
