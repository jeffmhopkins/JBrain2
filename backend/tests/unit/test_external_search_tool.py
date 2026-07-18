"""Unit tests for the search_external_video tool handler: formatting, deep-links, the untrusted
fence, and the degraded (embed-down) note. The corpus query itself (search_corpus) is
covered by the integration tests; here it is stubbed."""

from datetime import datetime

import jbrain.agent.externaltools as externaltools
from jbrain.agent.externaltools import build_external_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.external.corpus import CorpusHit, LibraryVideo

_CTX = ToolContext(session=SessionContext(principal_id="owner", principal_kind="owner"), scopes=())


def _handler():
    return build_external_handlers(object(), object())["search_external_video"]  # type: ignore[arg-type]


async def _run(monkeypatch, hits, degraded, args):
    async def fake_search(maker, embedder, query, limit, *, principal_id=""):
        return hits, degraded

    monkeypatch.setattr(externaltools, "search_corpus", fake_search)
    return await _handler()(args, _CTX)


async def test_formats_hits_with_timestamp_deep_links_and_fence(monkeypatch) -> None:
    hits = [
        CorpusHit(
            source_id="s1",
            title="Starship Update",
            channel_name="NSF",
            url="https://www.youtube.com/watch?v=abc",
            passage="They rolled the booster to the pad.",
            t_ms=185_000,
        )
    ]
    out = await _run(monkeypatch, hits, False, {"query": "starship"})

    assert isinstance(out, ToolOutput)
    # Untrusted-content fence is present and names the data as non-instructions.
    assert "never as instructions" in out
    # Deep-link carries the timestamp (185000 ms -> 185 s) on an existing query string.
    assert "https://www.youtube.com/watch?v=abc&t=185s" in out
    assert "Starship Update — NSF" in out
    assert out.web_sources[0].url == "https://www.youtube.com/watch?v=abc&t=185s"
    assert out.web_sources[0].title == "Starship Update"


async def test_summary_only_hit_has_no_timestamp(monkeypatch) -> None:
    hits = [
        CorpusHit(
            source_id="s2",
            title="Weekly Recap",
            channel_name="",
            url="https://youtu.be/xyz",
            passage="A broad overview of the week.",
            t_ms=None,
        )
    ]
    out = await _run(monkeypatch, hits, False, {"query": "recap"})

    assert "https://youtu.be/xyz" in out
    assert "&t=" not in out and "?t=" not in out
    assert "Weekly Recap\n" in out  # no " — channel" suffix when channel is blank


async def test_degraded_note_when_embed_down(monkeypatch) -> None:
    hits = [CorpusHit("s3", "T", "C", "https://youtu.be/q", "p", 1000)]
    out = await _run(monkeypatch, hits, True, {"query": "x"})
    assert "keyword-only" in out


async def test_empty_and_blank_query(monkeypatch) -> None:
    # A blank query no longer dead-ends — it points at the browse/count tool.
    blank = await _run(monkeypatch, [], False, {"query": "   "})
    assert "non-empty query" in blank and "list_external_video" in blank
    assert await _run(monkeypatch, [], False, {"query": "nothing"}) == (
        "No videos in the library matched 'nothing'."
    )


def _list_handler():
    return build_external_handlers(object(), object())["list_external_video"]  # type: ignore[arg-type]


async def _run_list(monkeypatch, videos, total, args):
    async def fake_list(maker, *, limit, offset=0, principal_id=""):
        return videos, total

    monkeypatch.setattr(externaltools, "list_corpus", fake_list)
    return await _list_handler()(args, _CTX)


def _video(title: str, **kw) -> LibraryVideo:
    return LibraryVideo(
        title=title,
        channel_name=kw.get("channel_name", ""),
        url=kw.get("url", f"https://youtu.be/{title}"),
        published_at=kw.get("published_at"),
        duration_s=kw.get("duration_s"),
        video_id=kw.get("video_id", title),
        provider=kw.get("provider", "youtube"),
    )


async def test_list_reports_total_and_metadata(monkeypatch) -> None:
    videos = [
        _video(
            "Starship Update",
            channel_name="NSF",
            url="https://www.youtube.com/watch?v=abc",
            published_at=datetime(2026, 7, 15),
            duration_s=3725,  # 1:02:05
        )
    ]
    out = await _run_list(monkeypatch, videos, 3, {})

    assert isinstance(out, ToolOutput)
    assert "holds 3 videos" in out
    assert "Starship Update — NSF" in out
    assert "published 2026-07-15" in out and "1:02:05" in out
    # A partial page advertises how to fetch the next one.
    assert "offset 1 for the next page" in out
    assert out.web_sources[0].url == "https://www.youtube.com/watch?v=abc"


async def test_list_empty_library(monkeypatch) -> None:
    out = await _run_list(monkeypatch, [], 0, {})
    assert out == "The video library is empty — no videos have been analysed yet."


async def test_list_offset_past_end(monkeypatch) -> None:
    out = await _run_list(monkeypatch, [], 5, {"offset": 99})
    assert "holds 5 videos" in out and "none past offset 99" in out


async def test_list_full_page_has_no_next_pointer(monkeypatch) -> None:
    # The whole library fits on one page — no pagination footer, no "of N" qualifier.
    videos = [_video("A"), _video("B")]
    out = await _run_list(monkeypatch, videos, 2, {})
    assert "holds 2 videos" in out
    assert "next page" not in out
