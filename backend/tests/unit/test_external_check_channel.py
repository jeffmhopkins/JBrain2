"""Unit tests for the check_channel tool handler: title filtering, corpus-dedup, the per-video
metadata enrichment (duration/publish-date/description), the recency window, bad input, and
formatting. The channel lister, the metadata resolver, and the corpus dedup are stubbed here
(network + DB are covered by the integration tests)."""

from datetime import UTC, datetime, timedelta

import jbrain.agent.externaltools as externaltools
from jbrain.agent.externaltools import build_external_handlers
from jbrain.agent.loop import ToolContext
from jbrain.db.session import SessionContext
from jbrain.stream import ChannelVideo, StreamError, VideoMeta

_CTX = ToolContext(session=SessionContext(principal_id="owner", principal_kind="owner"), scopes=())


def _uploads(*items: tuple[str, str]) -> list[ChannelVideo]:
    return [
        ChannelVideo(video_id=v, title=t, url=f"https://www.youtube.com/watch?v={v}")
        for v, t in items
    ]


def _handler(uploads, *, raise_exc=None, metas=None):
    def lister(channel_id, *, limit=10):
        if raise_exc is not None:
            raise raise_exc
        return uploads[:limit]

    def resolver(video_id, *, skip_guard=False):
        return (metas or {}).get(video_id)

    # object() stands in for the maker/embedder — check_channel touches neither (dedup + resolver
    # are stubbed), so the fakes never get used; the per-arg ignores stay put under ruff wrapping.
    return build_external_handlers(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        lister,
        meta_resolver=resolver,
    )["check_channel"]


async def _fresh(monkeypatch, keep: set[str]):
    async def fake_filter(maker, provider, video_ids, *, principal_id=""):
        return {v for v in video_ids if v in keep}

    monkeypatch.setattr(externaltools, "filter_new_video_ids", fake_filter)


async def test_returns_new_videos_not_in_corpus(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v2"})  # v1 already in the library
    out = await _handler(_uploads(("v1", "Old Recap"), ("v2", "Starship Static Fire")))(
        {"channel_id": "UCabc"}, _CTX
    )
    assert "v2" in out and "Starship Static Fire" in out
    assert "v1" not in out and "1 new video" in out


async def test_title_filter_is_case_insensitive_substring(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1", "v2"})
    out = await _handler(_uploads(("v1", "STARSHIP update"), ("v2", "Falcon 9 launch")))(
        {"channel_id": "UCabc", "title_include": "starship"}, _CTX
    )
    assert "v1" in out and "v2" not in out


async def test_title_filter_accepts_a_list_of_phrases(monkeypatch) -> None:
    """A list of phrases is OR-matched — an upload is kept if its title contains ANY of them."""
    await _fresh(monkeypatch, {"v1", "v2", "v3"})
    out = await _handler(
        _uploads(("v1", "Starship Update"), ("v2", "Starbase Flyover"), ("v3", "Falcon 9 launch"))
    )({"channel_id": "UCabc", "title_include": ["starship update", "starbase"]}, _CTX)
    assert "v1" in out and "v2" in out and "v3" not in out


async def test_listing_shows_duration_publish_and_description(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})
    meta = VideoMeta(
        duration_s=42 * 60 + 7,
        published_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        description="This week in spaceflight: a Starship static fire and more.\n\nSubscribe!",
    )
    out = await _handler(_uploads(("v1", "NSF Live")), metas={"v1": meta})(
        {"channel_id": "UCabc"}, _CTX
    )
    assert "42:07" in out  # duration rendered h/m/s
    assert "published 2026-07-20" in out
    assert "This week in spaceflight" in out  # description teaser, newlines collapsed
    assert "\n\n" not in out.split("This week", 1)[1][:40]


async def test_was_live_upload_is_tagged(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})
    meta = VideoMeta(duration_s=3 * 3600, was_live=True, aspect_ratio=1.78)
    out = await _handler(_uploads(("v1", "24/7 Starbase Live")), metas={"v1": meta})(
        {"channel_id": "UCabc"}, _CTX
    )
    assert "was live" in out


async def test_vertical_short_is_tagged_but_long_vertical_is_not(monkeypatch) -> None:
    await _fresh(monkeypatch, {"short", "tall"})
    metas = {
        "short": VideoMeta(duration_s=45, aspect_ratio=0.56),  # vertical + brief → Short?
        "tall": VideoMeta(duration_s=1800, aspect_ratio=0.56),  # vertical but 30 min → not tagged
    }
    out = await _handler(_uploads(("short", "Clip"), ("tall", "Vertical Doc")), metas=metas)(
        {"channel_id": "UCabc"}, _CTX
    )
    short_line = next(ln for ln in out.splitlines() if "Clip" in ln)
    tall_line = next(ln for ln in out.splitlines() if "Vertical Doc" in ln)
    assert "Short?" in short_line
    assert "Short?" not in tall_line


async def test_landscape_episode_is_not_tagged_short(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})
    meta = VideoMeta(duration_s=90, aspect_ratio=1.78)  # brief but 16:9 → not a Short
    out = await _handler(_uploads(("v1", "News Bite")), metas={"v1": meta})(
        {"channel_id": "UCabc"}, _CTX
    )
    assert "Short?" not in out and "was live" not in out


async def test_description_teaser_is_capped(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})
    meta = VideoMeta(description="x" * 1000)
    out = await _handler(_uploads(("v1", "Long blurb")), metas={"v1": meta})(
        {"channel_id": "UCabc"}, _CTX
    )
    assert "…" in out and "x" * 500 not in out


async def test_published_within_days_drops_old_uploads(monkeypatch) -> None:
    await _fresh(monkeypatch, {"recent", "old"})
    now = datetime.now(UTC)
    metas = {
        "recent": VideoMeta(published_at=now - timedelta(days=2)),
        "old": VideoMeta(published_at=now - timedelta(days=30)),
    }
    out = await _handler(_uploads(("recent", "New Update"), ("old", "Old Update")), metas=metas)(
        {"channel_id": "UCabc", "published_within_days": 7}, _CTX
    )
    assert "recent" in out and "New Update" in out
    assert "old" not in out and "Old Update" not in out


async def test_within_days_keeps_uploads_with_unknown_date(monkeypatch) -> None:
    """Fail open: an upload whose publish time didn't resolve is shown, not hidden by the window."""
    await _fresh(monkeypatch, {"v1"})
    out = await _handler(_uploads(("v1", "No date")), metas={"v1": VideoMeta()})(
        {"channel_id": "UCabc", "published_within_days": 7}, _CTX
    )
    assert "v1" in out and "No date" in out


async def test_within_days_none_left(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})
    old = VideoMeta(published_at=datetime.now(UTC) - timedelta(days=60))
    out = await _handler(_uploads(("v1", "Old")), metas={"v1": old})(
        {"channel_id": "UCabc", "published_within_days": 7}, _CTX
    )
    assert "last 7 day(s)" in out


async def test_meta_resolve_failure_still_lists_the_video(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})  # resolver returns None (no metas mapping)
    out = await _handler(_uploads(("v1", "Starship Update")))({"channel_id": "UCabc"}, _CTX)
    assert "v1" in out and "Starship Update" in out


async def test_all_already_ingested(monkeypatch) -> None:
    await _fresh(monkeypatch, set())  # nothing fresh
    out = await _handler(_uploads(("v1", "A"), ("v2", "B")))({"channel_id": "UCabc"}, _CTX)
    assert "already in the library" in out


async def test_no_uploads_match_title(monkeypatch) -> None:
    await _fresh(monkeypatch, {"v1"})
    out = await _handler(_uploads(("v1", "Falcon")))(
        {"channel_id": "UCabc", "title_include": "starship"}, _CTX
    )
    assert "No recent uploads" in out and "starship" in out


async def test_bad_and_missing_channel_id(monkeypatch) -> None:
    await _fresh(monkeypatch, set())
    h = _handler(_uploads())
    assert "needs a channel_id" in await h({"channel_id": "  "}, _CTX)
    assert "not a URL" in await h({"channel_id": "https://youtube.com/x"}, _CTX)


async def test_lister_error_is_surfaced(monkeypatch) -> None:
    await _fresh(monkeypatch, set())
    out = await _handler([], raise_exc=StreamError("that channel couldn't be listed"))(
        {"channel_id": "UCabc"}, _CTX
    )
    assert out == "that channel couldn't be listed"
