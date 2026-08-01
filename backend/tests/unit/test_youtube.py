"""The lightweight YouTube view behind web_fetch (jbrain.web.youtube). The yt-dlp resolver
and caption fetcher are injected, so these run with fakes — no yt-dlp, no network."""

from jbrain.captions import CaptionTrack
from jbrain.stream import ResolvedStream, StreamError
from jbrain.transcribe import Transcript
from jbrain.web.fetch import is_youtube_url
from jbrain.web.youtube import youtube_page


def _resolved(**over) -> ResolvedStream:  # type: ignore[no-untyped-def]
    base = dict(
        media_url="https://media.example/v.mp4",
        title="Rocket Launch Recap",
        is_live=False,
        duration_s=125.0,
        webpage_url="https://www.youtube.com/watch?v=abc123",
        channel_name="Space Channel",
        description="A recap of the launch.",
        caption=CaptionTrack(url="https://c.example/cc", ext="json3", kind="auto", lang="en"),
    )
    base.update(over)
    return ResolvedStream(**base)  # type: ignore[arg-type]


async def _run_blocking(fn, *args):  # type: ignore[no-untyped-def]
    return fn(*args)  # tests are sync; call the "blocking" resolver inline


async def test_is_youtube_url_matches_watch_and_short_links() -> None:
    assert is_youtube_url("https://www.youtube.com/watch?v=abc")
    assert is_youtube_url("https://youtu.be/abc")
    assert is_youtube_url("http://m.youtube.com/watch?v=abc")
    assert not is_youtube_url("https://en.wikipedia.org/wiki/YouTube")
    assert not is_youtube_url("https://notyoutube.com/watch")


async def test_youtube_page_composes_metadata_and_transcript() -> None:
    async def caption_fetcher(track, headers):  # type: ignore[no-untyped-def]
        assert track.ext == "json3"
        return Transcript(text="ten nine eight liftoff")

    out = await youtube_page(
        "https://youtu.be/abc123",
        resolver=lambda _url: _resolved(),
        caption_fetcher=caption_fetcher,
        run_blocking=_run_blocking,
    )
    assert out is not None
    title, md = out
    assert title == "Rocket Launch Recap"
    assert "Space Channel" in md
    assert "2:05" in md  # 125s rendered as duration
    assert "A recap of the launch." in md
    assert "## Transcript (captions)" in md
    assert "ten nine eight liftoff" in md


async def test_youtube_page_without_captions_points_at_analyze_video() -> None:
    out = await youtube_page(
        "https://youtu.be/abc123",
        resolver=lambda _url: _resolved(caption=None),
        caption_fetcher=None,  # never called when there's no track  # type: ignore[arg-type]
        run_blocking=_run_blocking,
    )
    assert out is not None
    _, md = out
    assert "No captions were available" in md
    assert "analyze_video" in md
    assert "A recap of the launch." in md  # description still present


async def test_youtube_page_returns_none_when_unresolvable() -> None:
    def boom(_url):  # type: ignore[no-untyped-def]
        raise StreamError("private video")

    out = await youtube_page(
        "https://youtu.be/gone",
        resolver=boom,
        caption_fetcher=None,  # type: ignore[arg-type]
        run_blocking=_run_blocking,
    )
    assert out is None  # the tool falls back to a normal HTML fetch


async def test_youtube_page_survives_a_caption_fetch_failure() -> None:
    async def caption_fetcher(track, headers):  # type: ignore[no-untyped-def]
        raise RuntimeError("caption host down")

    out = await youtube_page(
        "https://youtu.be/abc123",
        resolver=lambda _url: _resolved(),
        caption_fetcher=caption_fetcher,
        run_blocking=_run_blocking,
    )
    assert out is not None
    _, md = out
    # The transcript drops out but the metadata view still renders (best-effort).
    assert "No captions were available" in md
    assert "Space Channel" in md
