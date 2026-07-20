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
    MAX_FULL_FRAMES,
    ResolvedStream,
    StreamError,
    _full_frame_count,
    _header_args,
    _input_guard_args,
    _select_media,
    guard_public_host_or_stream,
    resolve_stream,
    sample_stream,
    sample_stream_full,
    ytdlp_available,
)


def test_full_frame_count_uses_interval_density_when_given() -> None:
    # interval_s (> 0) → one frame every N seconds, scaling with the video's length,
    # bounded by MAX_FULL_FRAMES; a frame every 30 s of a 600 s video = 20.
    assert _full_frame_count(frames=16, interval_s=30.0, duration=600.0) == 20
    assert _full_frame_count(frames=16, interval_s=60.0, duration=600.0) == 10
    # A fine density on a normal-length video is HONOURED, not silently coarsened to a
    # flat 60: a frame every 2 s of a ~15-min video is ~448, under the raised ceiling.
    assert _full_frame_count(frames=16, interval_s=2.0, duration=896.0) == 448
    # A dense interval on a very long video still clamps, so cost stays bounded.
    assert _full_frame_count(frames=16, interval_s=1.0, duration=100000.0) == MAX_FULL_FRAMES
    assert _full_frame_count(frames=16, interval_s=99999.0, duration=600.0) == 1  # ≥1 always


def test_full_frame_count_falls_back_to_flat_total_without_interval() -> None:
    # No interval → the flat `frames` total, clamped to the in-turn budget MAX_FRAMES.
    assert _full_frame_count(frames=8, interval_s=0.0, duration=600.0) == 8
    assert _full_frame_count(frames=1000, interval_s=0.0, duration=600.0) == MAX_FRAMES


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
        "extractor": "youtube",
        "id": "abc123",
    }
    r = _select_media(info, fallback_url="https://youtube.com/live/abc")
    assert r.media_url == "https://cdn.example.com/v.m3u8"
    assert r.title == "Launch" and r.is_live is True and r.duration_s is None
    # Provider + id are captured so a YouTube card can embed the synced player.
    assert r.provider == "youtube" and r.video_id == "abc123"


def test_select_media_wires_automatic_caption_track() -> None:
    # A YouTube info dict with only ASR (`automatic_captions`) yields a `caption` track on the
    # resolved stream (kind=auto) — the wiring the captions-first path depends on.
    info = {
        "url": "https://cdn/v.mp4",
        "title": "T",
        "id": "x",
        "extractor": "youtube",
        "automatic_captions": {
            "en": [{"url": "https://youtube.com/api/timedtext?x", "ext": "json3"}]
        },
    }
    r = _select_media(info, fallback_url="x")
    assert r.caption is not None and r.caption.kind == "auto" and r.caption.lang == "en"


def test_resolve_stream_requests_caption_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression for the whisper-always bug: the resolve opts MUST ask yt-dlp to surface caption
    # tracks, else the info dict comes back caption-less and every video falls through to whisper.
    import yt_dlp

    captured: dict = {}

    class FakeYDL:
        def __init__(self, opts: dict) -> None:
            captured.update(opts)

        def __enter__(self) -> "FakeYDL":
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def extract_info(self, url: str, download: bool = False) -> dict:
            return {
                "url": "https://cdn.example.com/v.mp4",
                "title": "T",
                "id": "x",
                "webpage_url": url,
                "extractor": "youtube",
                "automatic_captions": {"en": [{"url": "https://x/timedtext", "ext": "json3"}]},
            }

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    r = resolve_stream("https://www.youtube.com/watch?v=x", skip_guard=True)
    assert captured.get("writesubtitles") is True
    assert captured.get("writeautomaticsub") is True
    assert "en" in captured.get("subtitleslangs", [])
    assert r.caption is not None and r.caption.kind == "auto"  # wired through to the stream


def test_select_media_captures_http_headers() -> None:
    # yt-dlp's request headers ride along so ffmpeg fetches the signed URL as yt-dlp did
    # (else a windowed googlevideo read 403s).
    info = {"url": "https://cdn/v.mp4", "http_headers": {"User-Agent": "yt/1.0"}, "title": "T"}
    assert _select_media(info, fallback_url="x").http_headers == {"User-Agent": "yt/1.0"}
    # A merged A/V selection: prefer the chosen format's own headers over the top-level.
    info2 = {
        "requested_formats": [{"url": "https://cdn/v.mp4", "http_headers": {"User-Agent": "fmt"}}],
        "http_headers": {"User-Agent": "top"},
    }
    assert _select_media(info2, fallback_url="x").http_headers == {"User-Agent": "fmt"}


def test_header_args_sends_user_agent_and_extras_to_ffmpeg() -> None:
    args = _header_args({"User-Agent": "ANDROID_VR/1.0", "Accept": "*/*", "X-Foo": "bar"})
    assert args[args.index("-user_agent") + 1] == "ANDROID_VR/1.0"
    hdr = args[args.index("-headers") + 1]
    assert "Accept: */*\r\n" in hdr and "X-Foo: bar\r\n" in hdr
    assert "User-Agent" not in hdr  # UA goes via -user_agent, never duplicated
    assert _header_args({}) == []  # no headers → no args (a local file adds nothing)


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


def test_jpeg_thumbnail_downscales_and_survives_garbage() -> None:
    import io

    from PIL import Image

    from jbrain.media import jpeg_thumbnail

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (10, 120, 200)).save(buf, format="JPEG")
    original = buf.getvalue()
    thumb = jpeg_thumbnail(original, max_edge=320)
    with Image.open(io.BytesIO(thumb)) as img:
        assert max(img.size) <= 320  # downscaled to the card size
    assert len(thumb) < len(original)  # and smaller on the wire
    # Undecodable bytes degrade to the input rather than raising.
    assert jpeg_thumbnail(b"not a jpeg") == b"not a jpeg"


def test_url_input_restricted_to_network_protocols() -> None:
    # A URL media input gets a protocol whitelist barring file:/pipe:/concat:/data:
    # (a crafted manifest can't make ffmpeg open a local-file/exfil target).
    args = _input_guard_args("https://cdn.example.com/live.m3u8")
    assert args[0] == "-protocol_whitelist"
    protos = args[1].split(",")
    assert "https" in protos and "hls" in protos
    assert "file" not in protos and "pipe" not in protos and "concat" not in protos
    # A local file path (tests) is left unrestricted so ffmpeg can read it.
    assert _input_guard_args("/tmp/clip.mp4") == []


# ---- ffmpeg extraction against a synthetic clip -----------------------------

pytestmark_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg/ffprobe not installed"
)


def _make_clip(tmp_path, *, seconds: int = 5, with_audio: bool = False) -> str:
    """A synthetic clip (moving testsrc, optional sine audio), written to disk. Its
    path stands in for a resolved media URL — ffmpeg reads a file path the same way
    it reads an http(s) media URL, so sampling is exercised end-to-end offline."""
    out = tmp_path / ("av.mp4" if with_audio else "v.mp4")
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=duration={seconds}:size=320x240:rate=15",
    ]
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
async def test_single_grab_returns_one_frame(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path))
    sample = await sample_stream(r, frames=1, window_s=0)
    assert len(sample.frames) == 1
    assert sample.frames[0].timestamp_ms == 0
    assert sample.frames[0].jpeg[:2] == b"\xff\xd8"  # JPEG SOI
    assert sample.audio_wav == b""


def _make_black_intro_clip(tmp_path, *, black_s: int = 2, content_s: int = 3) -> str:
    """A clip that is fully BLACK for `black_s` seconds then testsrc content — the shape
    that made a single grab at t=0 return black while a seeked grab is fine. Reproduces
    the analyze_stream single-mode failure (a black intro/fade) offline."""
    out = tmp_path / "intro.mp4"
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=320x240:r=15:d={black_s}",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=320x240:rate=15:duration={content_s}",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return str(out)


def _mean_luma(jpeg: bytes) -> float:
    import io

    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(jpeg)) as img:
        return ImageStat.Stat(img.convert("L")).mean[0]


@pytestmark_ffmpeg
async def test_single_grab_honors_seek_past_black_intro(tmp_path) -> None:
    # The reported bug: single mode grabbed t=0 (a black intro), so a seek to real
    # content still came back black. With seek honored, a grab past the intro is content.
    r = _resolved(_make_black_intro_clip(tmp_path, black_s=2, content_s=3), duration=5.0)
    at_start = await sample_stream(r, frames=1, window_s=0, seek_s=0.0)
    seeked = await sample_stream(r, frames=1, window_s=0, seek_s=3.0)
    assert at_start.frames and _mean_luma(at_start.frames[0].jpeg) <= 12.0  # intro is black
    assert seeked.frames and _mean_luma(seeked.frames[0].jpeg) > 12.0  # seek moved off it


@pytestmark_ffmpeg
async def test_black_grab_retries_to_content(tmp_path) -> None:
    # A grab landing inside the black intro retries a beat later and prefers the non-black
    # result, so a transient fade isn't reported as "the video is black." seek=1.0 lands in
    # the 2s black intro; the +1.0s retry reaches content at ~2s.
    r = _resolved(_make_black_intro_clip(tmp_path, black_s=2, content_s=3), duration=5.0)
    sample = await sample_stream(r, frames=1, window_s=0, seek_s=1.0)
    assert sample.frames and _mean_luma(sample.frames[0].jpeg) > 12.0


@pytestmark_ffmpeg
async def test_window_returns_multiple_stamped_frames(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=5))
    # dedup off here: testsrc is coarse enough that the dHash collapses it (dedup is
    # exercised by the media suite) — this test isolates window spacing + stamping.
    sample = await sample_stream(r, frames=4, window_s=4, dedup_distance=0)
    assert 3 <= len(sample.frames) <= 4  # ~1 fps across a 4s window
    stamps = [f.timestamp_ms for f in sample.frames]
    assert stamps == sorted(stamps) and stamps[0] == 0  # window-relative, ascending
    assert stamps[1] == pytest.approx(1000, abs=200)  # ≈1s apart at fps=1


@pytestmark_ffmpeg
async def test_frame_count_capped(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=6))
    sample = await sample_stream(r, frames=1000, window_s=6, dedup_distance=0)
    assert 0 < len(sample.frames) <= MAX_FRAMES  # frames param clamped to the budget


@pytestmark_ffmpeg
async def test_full_samples_across_whole_vod(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=6), duration=6.0)
    sample = await sample_stream_full(r, frames=4, dedup_distance=0)
    assert len(sample.frames) == 4  # one per even bucket, discrete seek-grabs
    stamps = [f.timestamp_ms for f in sample.frames]
    assert stamps == sorted(stamps)  # ascending true offsets
    assert stamps[0] > 0 and stamps[-1] < 6000  # midpoints, inside the clip
    assert all(f.jpeg[:2] == b"\xff\xd8" for f in sample.frames)


async def test_full_refuses_live_and_unknown_duration() -> None:
    # No ffmpeg needed: the refusal precedes any sampling.
    with pytest.raises(StreamError):
        await sample_stream_full(_resolved("x", is_live=True, duration=None), frames=4)
    with pytest.raises(StreamError):
        await sample_stream_full(_resolved("x", is_live=False, duration=None), frames=4)


@pytestmark_ffmpeg
async def test_full_audio_when_short_enough(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=4, with_audio=True), duration=4.0)
    sample = await sample_stream_full(r, frames=2, want_audio=True, dedup_distance=0)
    assert sample.audio_wav[:4] == b"RIFF"  # whole-track WAV under the in-turn cap


@pytestmark_ffmpeg
async def test_audio_extracted_when_requested(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=4, with_audio=True))
    sample = await sample_stream(r, frames=2, window_s=3, want_audio=True)
    assert sample.audio_wav[:4] == b"RIFF"  # a real WAV, not header-only
    assert len(sample.audio_wav) > 1000


@pytestmark_ffmpeg
async def test_audio_empty_when_media_has_no_track(tmp_path) -> None:
    r = _resolved(_make_clip(tmp_path, seconds=4, with_audio=False))
    sample = await sample_stream(r, frames=2, window_s=3, want_audio=True)
    assert sample.audio_wav == b""  # video-only media degrades to frames-only


@pytestmark_ffmpeg
async def test_unreadable_media_returns_empty(tmp_path) -> None:
    bogus = tmp_path / "notavideo.mp4"
    bogus.write_bytes(b"this is not a video")
    sample = await sample_stream(_resolved(str(bogus)), frames=3, window_s=2)
    assert sample.frames == [] and sample.audio_wav == b""
