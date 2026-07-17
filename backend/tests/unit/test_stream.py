"""URL-sourced stream/video sampling (jbrain.stream): yt-dlp info selection, the
SSRF guard on a resolved media host, and ffmpeg frame/audio extraction over a
bounded window. Frame/audio tests run ffmpeg against a synthetic local clip (no
network, no real yt-dlp resolve) and skip when ffmpeg isn't on PATH; the
selection/guard tests are pure and always run."""

import subprocess

import pytest

from jbrain.media import ffmpeg_available
from jbrain.stream import (
    MAX_FRAMES,
    ResolvedStream,
    StreamError,
    _select_media,
    guard_public_host_or_stream,
    sample_stream,
    ytdlp_available,
)

# ---- pure selection + guard (no ffmpeg, no network) -------------------------


def test_ytdlp_available() -> None:
    assert ytdlp_available() is True  # a normal backend dependency


def test_select_media_direct_url() -> None:
    info = {
        "url": "https://cdn.example.com/v.m3u8",
        "title": "Launch",
        "is_live": True,
        "duration": None,
        "webpage_url": "https://youtube.com/live/abc",
    }
    r = _select_media(info, fallback_url="https://youtube.com/live/abc")
    assert r.media_url == "https://cdn.example.com/v.m3u8"
    assert r.title == "Launch" and r.is_live is True and r.duration_s is None


def test_select_media_requested_formats_fallback() -> None:
    """A merged A/V selection carries no top-level url — take the video leg's url."""
    info = {
        "requested_formats": [{"url": "https://cdn.example.com/video.mp4"}, {"url": "a"}],
        "title": "Clip",
        "duration": 12,
    }
    r = _select_media(info, fallback_url="x")
    assert r.media_url == "https://cdn.example.com/video.mp4"
    assert r.duration_s == 12.0 and r.is_live is False


def test_select_media_unwraps_playlist_entry() -> None:
    info = {"entries": [None, {"url": "https://cdn.example.com/e.mp4", "title": "E"}]}
    r = _select_media(info, fallback_url="x")
    assert r.media_url == "https://cdn.example.com/e.mp4"


def test_select_media_rejects_empty() -> None:
    for bad in (None, {}, {"entries": []}, {"title": "no media"}):
        with pytest.raises(StreamError):
            _select_media(bad, fallback_url="x")


def test_guard_refuses_private_resolved_host() -> None:
    # A resolved media URL pointing back at the box is an SSRF attempt — refused.
    with pytest.raises(StreamError):
        guard_public_host_or_stream("http://127.0.0.1:8080/live.m3u8", skip_dns=False)
    with pytest.raises(StreamError):
        guard_public_host_or_stream("ftp://example.com/x", skip_dns=False)
    # With skip_dns (test path, no resolution) a well-formed public URL passes.
    guard_public_host_or_stream("https://cdn.example.com/v.m3u8", skip_dns=True)


# ---- ffmpeg extraction against a synthetic clip -----------------------------

pytestmark_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg/ffprobe not installed"
)


def _make_clip(tmp_path, *, seconds: int = 5, with_audio: bool = False) -> str:
    """A synthetic clip (moving testsrc, optional sine audio), written to disk. Its
    path stands in for a resolved media URL — ffmpeg reads a file path the same way
    it reads an http(s) media URL, so sampling is exercised end-to-end offline."""
    out = tmp_path / ("av.mp4" if with_audio else "v.mp4")
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
           f"testsrc=duration={seconds}:size=320x240:rate=15"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-pix_fmt", "yuv420p", "-shortest", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return str(out)


def _resolved(url: str, *, is_live: bool = False, duration: float | None = 5.0) -> ResolvedStream:
    return ResolvedStream(
        media_url=url, title="t", is_live=is_live, duration_s=duration, webpage_url="w"
    )


@pytestmark_ffmpeg
def test_single_grab_returns_one_frame(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path))
    sample = sample_stream(r, frames=1, window_s=0)
    assert len(sample.frames) == 1
    assert sample.frames[0].timestamp_ms == 0
    assert sample.frames[0].jpeg[:2] == b"\xff\xd8"  # JPEG SOI
    assert sample.audio_wav == b""


@pytestmark_ffmpeg
def test_window_returns_multiple_stamped_frames(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=5))
    # dedup off here: testsrc is coarse enough that the dHash collapses it (dedup is
    # exercised by the media suite) — this test isolates window spacing + stamping.
    sample = sample_stream(r, frames=4, window_s=4, dedup_distance=0)
    assert 3 <= len(sample.frames) <= 4  # ~1 fps across a 4s window
    stamps = [f.timestamp_ms for f in sample.frames]
    assert stamps == sorted(stamps) and stamps[0] == 0  # window-relative, ascending
    assert stamps[1] == pytest.approx(1000, abs=200)  # ≈1s apart at fps=1


@pytestmark_ffmpeg
def test_frame_count_capped(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=6))
    sample = sample_stream(r, frames=1000, window_s=6, dedup_distance=0)
    assert 0 < len(sample.frames) <= MAX_FRAMES  # frames param clamped to the budget


@pytestmark_ffmpeg
def test_audio_extracted_when_requested(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=4, with_audio=True))
    sample = sample_stream(r, frames=2, window_s=3, want_audio=True)
    assert sample.audio_wav[:4] == b"RIFF"  # a real WAV, not header-only
    assert len(sample.audio_wav) > 1000


@pytestmark_ffmpeg
def test_audio_empty_when_media_has_no_track(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=4, with_audio=False))
    sample = sample_stream(r, frames=2, window_s=3, want_audio=True)
    assert sample.audio_wav == b""  # video-only media degrades to frames-only


@pytestmark_ffmpeg
def test_unreadable_media_returns_empty(tmp_path) -> None:
    bogus = tmp_path / "notavideo.mp4"
    bogus.write_bytes(b"this is not a video")
    sample = sample_stream(_resolved(str(bogus)), frames=3, window_s=2)
    assert sample.frames == [] and sample.audio_wav == b""
