"""The native-video transcode (NATIVE_VIDEO_PLAN V0): the ffmpeg argv it builds and its errors,
with the subprocess faked, plus one real round trip when ffmpeg is on PATH."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jbrain import media
from jbrain.media import TranscodeError, transcode_for_native_video


def _write(path: str, data: bytes) -> None:
    Path(path).write_bytes(data)


def _fake_proc(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rc: int = 0,
    write: bytes = b"MKV",
    exc: Exception | None = None,
) -> list[list[str]]:
    seen: list[list[str]] = []

    async def run(cmd: list[str], *, timeout_s: float) -> tuple[int | None, bytes, bytes]:
        seen.append(cmd)
        if exc is not None:
            raise exc
        if write:
            _write(cmd[-1], write)
        return rc, b"", b"boom: invalid data"

    monkeypatch.setattr(media, "run_media_proc", run)
    return seen


def _arg(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


async def test_builds_a_silent_capped_mjpeg_matroska(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_proc(monkeypatch)
    out = await transcode_for_native_video(Path("/in/clip.mp4"), fps=1.0, max_seconds=60.0)
    assert out == b"MKV"
    (cmd,) = seen
    assert cmd[0] == "ffmpeg" and _arg(cmd, "-i") == "/in/clip.mp4"
    assert _arg(cmd, "-protocol_whitelist") == "file,pipe"
    assert cmd.index("-protocol_whitelist") < cmd.index("-i")
    assert _arg(cmd, "-t") == "60" and "-an" in cmd
    assert _arg(cmd, "-c:v") == "mjpeg" and _arg(cmd, "-q:v") == "4"
    assert _arg(cmd, "-f") == "matroska" and cmd[-1].endswith(".mkv")
    vf = _arg(cmd, "-vf")
    assert vf.startswith("fps=1,scale=") and "min(iw,1280)" in vf and "min(ih,1280)" in vf
    assert media.NATIVE_VIDEO_MEDIA_TYPE == "video/x-matroska"


async def test_honours_the_edge_and_length_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_proc(monkeypatch)
    await transcode_for_native_video(Path("/c"), fps=2.0, max_seconds=12.5, max_edge=640)
    assert _arg(seen[0], "-t") == "12.5"
    assert _arg(seen[0], "-vf").startswith("fps=2,") and "min(iw,640)" in _arg(seen[0], "-vf")


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"rc": 1}, "ffmpeg exited 1: boom"),
        ({"write": b""}, "no output"),
        ({"exc": TimeoutError()}, "timed out"),
        ({"exc": FileNotFoundError("ffmpeg")}, "could not start"),
    ],
)
async def test_failures_raise_a_transcode_error(
    monkeypatch: pytest.MonkeyPatch, kw: dict[str, Any], match: str
) -> None:
    _fake_proc(monkeypatch, **kw)
    with pytest.raises(TranscodeError, match=match):
        await transcode_for_native_video(Path("/c"), fps=1.0)


async def test_every_upload_read_is_held_to_local_protocols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uploaded clip is untrusted bytes: a crafted playlist must not make ffmpeg or ffprobe
    open anything but the local file it was handed."""
    seen = _fake_proc(monkeypatch, write=b"")
    monkeypatch.setattr(media, "ffmpeg_available", lambda: True)
    await media.probe_duration_s(Path("/c"))
    await media.sample_frames(b"not a video")
    tools = [cmd[0] for cmd in seen]
    assert tools == ["ffprobe", "ffprobe", "ffmpeg"]  # sample_frames probes, then extracts
    for cmd in seen:
        assert _arg(cmd, "-protocol_whitelist") == "file,pipe"
        source = cmd.index("-i") if "-i" in cmd else len(cmd) - 1
        assert cmd.index("-protocol_whitelist") < source


def _make_clip(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1600x900:rate=25",
         "-t", "5", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip


def _probe(path: Path) -> str:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
         "stream=codec_name,width,height,nb_read_frames", "-of", "default=nw=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


@pytest.mark.skipif(not media.ffmpeg_available(), reason="ffmpeg/ffprobe not installed")
def test_a_real_clip_comes_out_mjpeg_at_the_asked_rate(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    _make_clip(src)
    out = tmp_path / "out.mkv"
    out.write_bytes(asyncio.run(transcode_for_native_video(src, fps=1.0, max_seconds=3.0)))
    probe = _probe(out)
    assert "codec_name=mjpeg" in probe
    # Longest edge capped at 1280, aspect kept, both sides even; one frame a second.
    assert "width=1280" in probe and "height=720" in probe
    assert "nb_read_frames=3" in probe


async def test_native_clip_transcodes_only_a_short_known_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _fake_proc(monkeypatch)
    lengths = iter([30.0, 61.0, None])

    async def probe(path: Path) -> float | None:
        return next(lengths)

    monkeypatch.setattr(media, "probe_duration_s", probe)
    short = await media.native_clip(b"v", max_seconds=60.0, fps=1.0)
    assert short == media.NativeClip(seconds=30.0, data=b"MKV")
    assert await media.native_clip(b"v", max_seconds=60.0, fps=1.0) == media.NativeClip(61.0, None)
    assert await media.native_clip(b"v", max_seconds=60.0, fps=1.0) == media.NativeClip(None, None)
    assert len(seen) == 1  # only the qualifying clip reached ffmpeg


async def test_stream_clip_reads_the_url_under_its_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    from jbrain import stream

    seen = _fake_proc(monkeypatch)
    resolved = stream.ResolvedStream(
        media_url="https://cdn.example.com/v.mp4",
        title="t",
        is_live=False,
        duration_s=30.0,
        webpage_url="https://example.com/watch",
        http_headers={"User-Agent": "UA/1", "Referer": "https://example.com"},
    )
    assert await stream.native_stream_clip(resolved, max_seconds=60.0, fps=1.0) == b"MKV"
    (cmd,) = seen
    assert _arg(cmd, "-i") == "https://cdn.example.com/v.mp4"
    assert "file" not in _arg(cmd, "-protocol_whitelist").split(",")
    assert _arg(cmd, "-user_agent") == "UA/1" and "Referer" in _arg(cmd, "-headers")
    assert cmd.index("-rw_timeout") < cmd.index("-i") and _arg(cmd, "-t") == "60"
